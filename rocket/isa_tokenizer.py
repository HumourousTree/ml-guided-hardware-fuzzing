"""ISA-field tokenizer for RV64GC assembly programs (Track 1: the bug-trained generator).

One token per field instead of BPE pieces, so the model's vocabulary is the ISA itself:
  <nl>                         line separator            <bos> <eos> <pad> <unk>
  @def                         a label definition follows (then the label token)
  .dword .word .align ...      directives
  add fmadd.d c.addi csrrw ... one token per mnemonic
  x0..x31 f0..f31              registers (ABI names are mapped to these)
  csr:mstatus ...              CSR names; rm:rne ...       rounding modes; arg:norvc ... directive arguments
  lbl:L3 lbl:T2_A lbl:1f ...   label references / names
  (  )                         memory operand brackets: imm(reg) -> <imm> ( reg )
  #+ #- 0..f #.                immediates, lossless: sign, hex digits (most significant first), end
  ch:x                         character fallback for anything else (an unseen symbol), ended by ch:end
Commas are implicit (operands are separated by their token boundaries). decode(encode(p)) == canon(p), where canon
writes integers in decimal and registers by number; this is checked by `python -m rocket.isa_tokenizer --check`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ABI = {"zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4, "t0": 5, "t1": 6, "t2": 7, "s0": 8, "fp": 8, "s1": 9,
       **{f"a{i}": 10 + i for i in range(8)}, **{f"s{i}": 16 + i for i in range(2, 12)},
       **{f"t{i}": 25 + i for i in range(3, 7)}}
FABI = {**{f"ft{i}": i for i in range(8)}, "fs0": 8, "fs1": 9, **{f"fa{i}": 10 + i for i in range(8)},
        **{f"fs{i}": 16 + i for i in range(2, 12)}, **{f"ft{i}": 20 + i for i in range(8, 12)}}
ROUNDING = ["rne", "rtz", "rdn", "rup", "rmm", "dyn"]
# CSRs the generators use, plus the standard machine-mode set (others fall back to characters)
CSRS = ["fflags", "frm", "fcsr", "cycle", "time", "instret", "mstatus", "misa", "medeleg", "mideleg", "mie", "mtvec",
        "mcounteren", "mscratch", "mepc", "mcause", "mtval", "mip", "mcycle", "minstret", "mvendorid", "marchid",
        "mimpid", "mhartid", "sstatus", "sie", "stvec", "sscratch", "sepc", "scause", "stval", "sip", "satp",
        "pmpcfg0", "pmpaddr0", "tselect", "tdata1", "tdata2", "dcsr", "dpc", "mcountinhibit"]
DIRECTIVES = [".align", ".dword", ".word", ".half", ".byte", ".globl", ".option", ".section", ".text", ".data",
              ".balign", ".zero", ".space"]
DIRECTIVE_ARGS = ["rvc", "norvc", "push", "pop", "arch", ".text", ".text.init", ".data", ".bss", ".tohost", '"aw"', '"ax"',
                  "@progbits"]
SPECIAL = ["<pad>", "<bos>", "<eos>", "<unk>", "<nl>", "@def", "(", ")", "#+", "#-", "#.", "ch:end"]
HEX = list("0123456789abcdef")
CHARS = [chr(c) for c in range(33, 127)]
# every mnemonic the generators can emit (RV64IMAFDC + Zicsr + pseudo-ops); unseen ones fall back to characters
MNEMONICS = """add addi addiw addw and andi auipc beq beqz bge bgeu bgez bgt bgtu bgtz ble bleu blez blt bltu bltz bne bnez
call csrc csrci csrr csrrc csrrci csrrs csrrsi csrrw csrrwi csrs csrsi csrw csrwi div divu divuw divw ebreak ecall
fence fence.i j jal jalr jr la lb lbu ld lh lhu li lr.d lr.w lui lw lwu mret mul mulh mulhsu mulhu mulw mv neg negw nop
not or ori rem remu remuw remw ret sb sc.d sc.w sd seqz sext.w sgtz sh sll slli slliw sllw slt slti sltiu sltu sltz snez
sra srai sraiw sraw sret srl srli srliw srlw sub subw sw wfi xor xori
amoadd.d amoadd.w amoand.d amoand.w amomax.d amomax.w amomaxu.d amomaxu.w amomin.d amomin.w amominu.d amominu.w
amoor.d amoor.w amoswap.d amoswap.w amoxor.d amoxor.w
fadd.d fadd.s fclass.d fclass.s fcvt.d.l fcvt.d.lu fcvt.d.s fcvt.d.w fcvt.d.wu fcvt.l.d fcvt.l.s fcvt.lu.d fcvt.lu.s
fcvt.s.d fcvt.s.l fcvt.s.lu fcvt.s.w fcvt.s.wu fcvt.w.d fcvt.w.s fcvt.wu.d fcvt.wu.s fdiv.d fdiv.s feq.d feq.s fld fle.d
fle.s flt.d flt.s flw fmadd.d fmadd.s fmax.d fmax.s fmin.d fmin.s fmsub.d fmsub.s fmul.d fmul.s fmv.d fmv.d.x fmv.s
fmv.w.x fmv.x.d fmv.x.w fneg.d fneg.s fabs.d fabs.s fnmadd.d fnmadd.s fnmsub.d fnmsub.s fsd fsgnj.d fsgnj.s fsgnjn.d
fsgnjn.s fsgnjx.d fsgnjx.s fsqrt.d fsqrt.s fsub.d fsub.s fsw frcsr fscsr frrm fsrm fsrmi frflags fsflags fsflagsi
c.add c.addi c.addi16sp c.addi4spn c.addiw c.addw c.and c.andi c.beqz c.bnez c.ebreak c.fld c.fldsp c.fsd c.fsdsp
c.j c.jalr c.jr c.ld c.ldsp c.li c.lui c.lw c.lwsp c.mv c.nop c.or c.sd c.sdsp c.slli c.srai c.srli c.sub c.subw c.sw
c.swsp c.xor""".split()
INT_RE = re.compile(r"^[+-]?(0x[0-9a-fA-F]+|\d+)$")
MEM_RE = re.compile(r"^([+-]?(?:0x[0-9a-fA-F]+|\d+))?\((\w+)\)$")
LABEL_RE = re.compile(r"^([A-Za-z_.$][\w.$]*|\d+):\s*(.*)$")
LOCAL_REF = re.compile(r"^\d+[fb]$")


class ISATokenizer:
    def __init__(self, extra_labels: int = 32):
        labels = ([f"lbl:L{i}" for i in range(extra_labels)] + [f"lbl:T{i}_A" for i in range(extra_labels)]
                  + [f"lbl:{i}" for i in range(1, 10)] + [f"lbl:{i}{d}" for i in range(1, 10) for d in "fb"]
                  + [f"lbl:{n}" for n in ("_start", "data", "trap", "tohost", "fromhost", "handler", "done", "end")])
        self.vocab = (SPECIAL + HEX + DIRECTIVES + MNEMONICS + [f"x{i}" for i in range(32)] + [f"f{i}" for i in range(32)]
                      + [f"arg:{d}" for d in DIRECTIVE_ARGS] + [f"csr:{c}" for c in CSRS] + [f"rm:{r}" for r in ROUNDING] + labels + [f"ch:{c}" for c in CHARS])
        self.id = {t: i for i, t in enumerate(self.vocab)}
        assert len(self.id) == len(self.vocab), "duplicate tokens"

    def __len__(self) -> int:
        return len(self.vocab)

    # ── encoding ──────────────────────────────────────────────────────────
    def _int(self, v: int) -> list[str]:
        return ["#-" if v < 0 else "#+", *f"{abs(v):x}", "#."]

    def _chars(self, s: str) -> list[str]:
        return [f"ch:{c}" if f"ch:{c}" in self.id else "<unk>" for c in s] + ["ch:end"]

    def _operand(self, op: str, mnemonic: str) -> list[str]:
        o = op.strip()
        lo = o.lower()
        if INT_RE.match(o):
            return self._int(int(o, 0))
        m = MEM_RE.match(o.replace(" ", ""))
        if m:
            return (self._int(int(m.group(1), 0)) if m.group(1) else []) + ["(", *self._operand(m.group(2), mnemonic), ")"]
        if re.fullmatch(r"x([0-9]|[12][0-9]|3[01])", lo): return [lo]
        if re.fullmatch(r"f([0-9]|[12][0-9]|3[01])", lo): return [lo]
        if lo in ABI: return [f"x{ABI[lo]}"]
        if lo in FABI and mnemonic.startswith(("f", "c.f")): return [f"f{FABI[lo]}"]
        if lo in ROUNDING: return [f"rm:{lo}"]
        if mnemonic.startswith("csr") or mnemonic in ("frcsr", "fscsr"):
            if f"csr:{lo}" in self.id: return [f"csr:{lo}"]
        if mnemonic.startswith(".") and f"arg:{o}" in self.id: return [f"arg:{o}"]
        if f"lbl:{o}" in self.id: return [f"lbl:{o}"]
        return self._chars(o)

    def encode_line(self, line: str) -> list[str]:
        text = line.split("#")[0].strip()
        out: list[str] = []
        while True:
            m = LABEL_RE.match(text)
            if not m or text.startswith("."):
                break
            name = m.group(1)
            out += ["@def", f"lbl:{name}" if f"lbl:{name}" in self.id else "<unk>"] if f"lbl:{name}" in self.id \
                else ["@def", *self._chars(name)]
            text = m.group(2).strip()
        if not text:
            return out
        parts = text.split(None, 1)
        mn = parts[0].lower()
        out += [mn] if mn in self.id else self._chars(mn)
        if len(parts) > 1:
            for op in re.split(r",(?![^(]*\))", parts[1]):
                if op.strip():
                    out += self._operand(op, mn)
        return out

    def encode(self, asm: str, bos: bool = True) -> list[int]:
        toks = ["<bos>"] if bos else []
        for line in asm.splitlines():
            t = self.encode_line(line)
            if t:
                toks += t + ["<nl>"]
        return [self.id[t] for t in toks + ["<eos>"]]

    # ── decoding ──────────────────────────────────────────────────────────
    def decode(self, ids: list[int]) -> str:
        toks = [self.vocab[i] for i in ids if self.vocab[i] not in ("<bos>", "<pad>")]
        lines, cur = [], []
        for t in toks:
            if t in ("<nl>", "<eos>"):
                if cur: lines.append(cur)
                cur = []
                if t == "<eos>": break
            else:
                cur.append(t)
        return "\n".join(self._decode_line(l) for l in lines) + "\n"

    def _decode_line(self, toks: list[str]) -> str:
        i, labels, items = 0, [], []

        def chars(j):
            s = []
            while j < len(toks) and toks[j] != "ch:end":
                s.append(toks[j][3:] if toks[j].startswith("ch:") else "?"); j += 1
            return "".join(s), j + 1

        def atom(j):
            t = toks[j]
            if t in ("#+", "#-"):
                k = j + 1
                while k < len(toks) and toks[k] in HEX: k += 1
                v = int("".join(toks[j + 1:k]) or "0", 16)
                return str(-v if t == "#-" else v), k + 1
            if t.startswith("ch:"): return chars(j)
            for p in ("csr:", "rm:", "lbl:", "arg:"):
                if t.startswith(p): return t[len(p):], j + 1
            return t, j + 1

        while i < len(toks) and toks[i] == "@def":
            name, i = atom(i + 1); labels.append(name + ":")
        if i >= len(toks):
            return " ".join(labels)
        mn, i = atom(i)
        ops = []
        while i < len(toks):
            if toks[i] == "(":
                reg, i = atom(i + 1); i += 1                     # skip ")"
                ops.append(f"({reg})"); continue
            numeric = toks[i] in ("#+", "#-")
            a, i = atom(i)
            if numeric and i < len(toks) and toks[i] == "(":     # imm(reg); a register is never merged
                reg, i = atom(i + 1); i += 1
                a = f"{a}({reg})"
            ops.append(a)
        body = mn + (" " + ", ".join(ops) if ops else "")
        return (" ".join(labels) + " " if labels else "") + ("  " if not labels else "") + body if labels else "  " + body

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps({"kind": "benchfuzz-isa-tokenizer", "version": 1, "vocab": self.vocab}))


def canon(asm: str) -> str:
    """Canonical text form used by the round-trip check (same rules as encode/decode)."""
    tk = ISATokenizer()
    return tk.decode(tk.encode(asm))


if __name__ == "__main__":
    import argparse
    import random
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import rv64fuzz as f

    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--n", type=int, default=100)
    a = ap.parse_args()
    tk = ISATokenizer()
    tpls = json.loads((Path(__file__).resolve().parent / "templates" / "llm_flash.json").read_text())
    tpls = tpls.get("templates", tpls) if isinstance(tpls, dict) else tpls
    bad = fallback = total = lines = 0
    for prof in ("base", "ext", "llmflash"):
        for s in range(a.n):
            asm = f.gen_program(random.Random(s), 40, profile=prof, templates=tpls if prof == "llmflash" else None)
            ids = tk.encode(asm)
            total += len(ids); lines += asm.count("\n")
            fallback += sum(1 for i in ids if tk.vocab[i].startswith("ch:") or tk.vocab[i] == "<unk>")
            once = tk.decode(ids)
            if tk.decode(tk.encode(once)) != once or once.count("\n") != len([l for l in asm.splitlines() if l.split("#")[0].strip()]):
                bad += 1
    print(json.dumps({"vocab": len(tk), "programs": 3 * a.n, "tokens": total, "tokens_per_line": round(total / lines, 2),
                      "fallback_token_share": round(fallback / total, 5), "roundtrip_failures": bad}))
