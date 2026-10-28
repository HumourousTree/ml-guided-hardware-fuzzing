"""Minimal RV32IM assembler: text -> machine words, with line-numbered errors for LLM syntax correction.

Supports labels, ABI register names, R/I/S/B/U/J formats, loads/stores `imm(rs)`, RV32M, and the pseudo-ops
nop li mv not neg seqz snez j jr ret beqz bnez bgt ble bgtu bleu.  Comments: `#` and `//`.
"""
import re


class AsmError(ValueError):
    def __init__(self, line_no: int, text: str, msg: str):
        super().__init__(f"line {line_no}: {msg}  [{text.strip()}]")
        self.line_no, self.text, self.msg = line_no, text, msg


_ABI = {"zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4, "t0": 5, "t1": 6, "t2": 7, "s0": 8, "fp": 8, "s1": 9,
        **{f"a{i}": 10 + i for i in range(8)}, **{f"s{i}": 16 + i for i in range(2, 12)},
        **{f"t{i}": 25 + i for i in range(3, 7)}}
_R = {"add": (0, 0), "sub": (0x20, 0), "sll": (0, 1), "slt": (0, 2), "sltu": (0, 3), "xor": (0, 4), "srl": (0, 5),
      "sra": (0x20, 5), "or": (0, 6), "and": (0, 7), "mul": (1, 0), "mulh": (1, 1), "mulhsu": (1, 2), "mulhu": (1, 3),
      "div": (1, 4), "divu": (1, 5), "rem": (1, 6), "remu": (1, 7)}
_I = {"addi": 0, "slti": 2, "sltiu": 3, "xori": 4, "ori": 6, "andi": 7}
_SH = {"slli": (0, 1), "srli": (0, 5), "srai": (0x20, 5)}
_L = {"lb": 0, "lh": 1, "lw": 2, "lbu": 4, "lhu": 5}
_S = {"sb": 0, "sh": 1, "sw": 2}
_B = {"beq": 0, "bne": 1, "blt": 4, "bge": 5, "bltu": 6, "bgeu": 7}


def _reg(tok: str, ln, text) -> int:
    t = tok.strip().lower()
    if t in _ABI: return _ABI[t]
    m = re.fullmatch(r"x(\d+)", t)
    if m and 0 <= int(m.group(1)) <= 31: return int(m.group(1))
    raise AsmError(ln, text, f"invalid register '{tok}' (use x0-x31 or ABI names)")


def _imm(tok: str, ln, text, lo=None, hi=None) -> int:
    try: v = int(tok.strip(), 0)
    except ValueError: raise AsmError(ln, text, f"invalid immediate '{tok}'")
    if lo is not None and not (lo <= v <= hi): raise AsmError(ln, text, f"immediate {v} out of range [{lo}, {hi}]")
    return v


def _mem(tok: str, ln, text):
    m = re.fullmatch(r"\s*(-?\w+)?\s*\(\s*(\w+)\s*\)\s*", tok)
    if not m: raise AsmError(ln, text, f"expected imm(reg), got '{tok}'")
    return _imm(m.group(1) or "0", ln, text, -2048, 2047), _reg(m.group(2), ln, text)


def _enc_r(f7, rs2, rs1, f3, rd): return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | 0x33
def _enc_i(imm, rs1, f3, rd, op): return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op
def _enc_s(imm, rs2, rs1, f3): return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((imm & 0x1F) << 7) | 0x23
def _enc_b(off, rs2, rs1, f3):
    off &= 0x1FFF
    return (((off >> 12) & 1) << 31) | (((off >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (((off >> 1) & 0xF) << 8) | (((off >> 11) & 1) << 7) | 0x63
def _enc_j(off, rd):
    off &= 0x1FFFFF
    return (((off >> 20) & 1) << 31) | (((off >> 1) & 0x3FF) << 21) | (((off >> 11) & 1) << 20) | (((off >> 12) & 0xFF) << 12) | (rd << 7) | 0x6F


def _li(rd, v):
    v &= 0xFFFFFFFF
    sv = v - (1 << 32) if v & 0x80000000 else v
    if -2048 <= sv <= 2047: return [_enc_i(sv, 0, 0, rd, 0x13)]
    hi, lo = (v + 0x800) >> 12 & 0xFFFFF, ((v & 0xFFF) ^ 0x800) - 0x800
    return [(hi << 12) | (rd << 7) | 0x37, _enc_i(lo, rd, 0, rd, 0x13)]


def assemble(source: str) -> list[int]:
    """Two-pass assembly. Raises AsmError (with line number) on the first problem."""
    items = []                                   # (line_no, text, mnemonic, operands)
    labels: dict[str, int] = {}
    pc = 0
    for ln, raw in enumerate(source.splitlines(), 1):
        text = re.split(r"#|//", raw)[0].strip()
        while True:
            m = re.match(r"^([A-Za-z_.$][\w.$]*)\s*:\s*(.*)$", text)
            if not m: break
            if m.group(1) in labels: raise AsmError(ln, raw, f"duplicate label '{m.group(1)}'")
            labels[m.group(1)] = pc; text = m.group(2).strip()
        if not text: continue
        parts = text.split(None, 1)
        mn = parts[0].lower()
        ops = [o.strip() for o in re.split(r",(?![^(]*\))", parts[1])] if len(parts) > 1 else []
        n = 1
        if mn == "li":
            if len(ops) != 2: raise AsmError(ln, raw, "li needs 2 operands")
            n = len(_li(0, _imm(ops[1], ln, raw)))
        items.append((ln, raw, mn, ops, pc)); pc += 4 * n
    words: list[int] = []

    def target(tok, at, ln, raw):
        if tok in labels: return labels[tok] - at
        try: return int(tok, 0)
        except ValueError: raise AsmError(ln, raw, f"unresolved label '{tok}'")

    for ln, raw, mn, ops, at in items:
        def need(k):
            if len(ops) != k: raise AsmError(ln, raw, f"'{mn}' needs {k} operand(s), got {len(ops)}")
        R = lambda i: _reg(ops[i], ln, raw)
        if mn in _R: need(3); f7, f3 = _R[mn]; words.append(_enc_r(f7, R(2), R(1), f3, R(0)))
        elif mn in _I: need(3); words.append(_enc_i(_imm(ops[2], ln, raw, -2048, 2047), R(1), _I[mn], R(0), 0x13))
        elif mn in _SH: need(3); f7, f3 = _SH[mn]; words.append(_enc_i((f7 << 5) | _imm(ops[2], ln, raw, 0, 31), R(1), f3, R(0), 0x13))
        elif mn in _L: need(2); i, r = _mem(ops[1], ln, raw); words.append(_enc_i(i, r, _L[mn], R(0), 0x03))
        elif mn in _S: need(2); i, r = _mem(ops[1], ln, raw); words.append(_enc_s(i, R(0), r, _S[mn]))
        elif mn in _B:
            need(3); off = target(ops[2], at, ln, raw)
            if off % 2 or not -4096 <= off <= 4094: raise AsmError(ln, raw, f"branch offset {off} out of range")
            words.append(_enc_b(off, R(1), R(0), _B[mn]))
        elif mn in ("bgt", "ble", "bgtu", "bleu"):
            need(3); off = target(ops[2], at, ln, raw)
            f3 = {"bgt": 4, "ble": 5, "bgtu": 6, "bleu": 7}[mn]; words.append(_enc_b(off, R(0), R(1), f3))
        elif mn in ("beqz", "bnez"): need(2); words.append(_enc_b(target(ops[1], at, ln, raw), 0, R(0), 0 if mn == "beqz" else 1))
        elif mn == "jal":
            if len(ops) == 1: ops = ["x1"] + ops
            need(2); words.append(_enc_j(target(ops[1], at, ln, raw), R(0)))
        elif mn == "j": need(1); words.append(_enc_j(target(ops[0], at, ln, raw), 0))
        elif mn == "jalr":
            if len(ops) == 1: words.append(_enc_i(0, R(0), 0, 1, 0x67))
            elif len(ops) == 2: i, r = _mem(ops[1], ln, raw); words.append(_enc_i(i, r, 0, R(0), 0x67))
            else: need(3); words.append(_enc_i(_imm(ops[2], ln, raw, -2048, 2047), R(1), 0, R(0), 0x67))
        elif mn == "jr": need(1); words.append(_enc_i(0, R(0), 0, 0, 0x67))
        elif mn == "ret": need(0); words.append(_enc_i(0, 1, 0, 0, 0x67))
        elif mn in ("lui", "auipc"):
            need(2); words.append((_imm(ops[1], ln, raw, 0, 0xFFFFF) << 12) | (R(0) << 7) | (0x37 if mn == "lui" else 0x17))
        elif mn == "nop": need(0); words.append(0x13)
        elif mn == "fence": words.append(0x0FF0000F)          # fence iorw, iorw (predecessor/successor sets ignored)
        elif mn == "li": need(2); words += _li(R(0), _imm(ops[1], ln, raw))
        elif mn == "mv": need(2); words.append(_enc_i(0, R(1), 0, R(0), 0x13))
        elif mn == "not": need(2); words.append(_enc_i(-1, R(1), 4, R(0), 0x13))
        elif mn == "neg": need(2); words.append(_enc_r(0x20, R(1), 0, 0, R(0)))
        elif mn == "seqz": need(2); words.append(_enc_i(1, R(1), 3, R(0), 0x13))
        elif mn == "snez": need(2); words.append(_enc_r(0, R(1), 0, 3, R(0)))
        else: raise AsmError(ln, raw, f"unknown or unsupported instruction '{mn}'")
    return words
