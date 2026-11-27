"""Corpus-derived ISA grammar for constrained decoding (Track 1).

The grammar is learned from the existing generators' bodies (per profile group: "base" = no trap handler, "trap" =
ext / llm profiles), at the level of the ISA-field tokens of rocket/isa_tokenizer.py:
  * the operand signatures of every mnemonic (X, F, C(sr), R(ounding mode), A(rg), L(abel), I(mmediate), M(emory))
  * per (mnemonic, operand position): the immediate range seen and its alignment (gcd), and for memory operands the
    base registers seen (so a load never addresses outside the data region the harness compares)
plus the harness rules: x30/x31 (and x29 with a trap handler) are never a destination, labels are only referenced
forward (defined after use, once). During sampling, `Constraint.allowed()` gives the tokens that keep the line inside
the grammar, so every sampled body is syntactically valid and inside the generator's operand ranges by construction.
"""
from __future__ import annotations

import collections
import json
import math
import random
import re
from pathlib import Path

from rocket.genmodel.data import PROFILES, CORES, body_of, tokenizer

HEXV = {c: i for i, c in enumerate("0123456789abcdef")}
WRITER_EXCEPT = ("sb", "sh", "sw", "sd", "fsw", "fsd", "c.sw", "c.sd", "c.fsw", "c.fsd", "c.swsp", "c.sdsp", "j", "c.j",
                 "c.jr", "csrw", "csrs", "csrc", "csrwi", "csrsi", "csrci", "fence", "fence.i", "ecall", "ebreak", "mret",
                 "c.nop", "nop", "c.beqz", "c.bnez", "c.ebreak", "wfi")


# legal ISA ranges for immediate (non-memory) fields: the learned range is widened to these, so random immediates are
# not limited to the values the grammar sample happened to contain (memory offsets keep the observed range)
S12 = (-2048, 2047)
LEGAL = {**{m: S12 for m in ("addi", "addiw", "andi", "ori", "xori", "slti", "sltiu")},
         **{m: (0, 63) for m in ("slli", "srli", "srai")}, **{m: (0, 31) for m in ("slliw", "srliw", "sraiw")},
         "lui": (0, 0xFFFFF), "auipc": (0, 0xFFFFF), "li": (-2**63, 2**64 - 1), "csrrwi": (0, 31),
         "csrrsi": (0, 31), "csrrci": (0, 31), "fsrmi": (0, 4), "c.addiw": (-32, 31), "c.li": (-32, 31),
         "c.andi": (-32, 31)}


EXCLUDED = {"c.lui"}   # legal immediates are two disjoint ranges (1-31, 0xfffe0-0xfffff): not expressible as one range


def group(profile: str) -> str:
    """Grammar group: each profile has its own harness context (only ext enables compressed code)."""
    return "llmflash" if profile == "llm" else profile


def _types(tk, toks: list[str]):
    """Operand type signature and per-operand details of one encoded line (after the mnemonic)."""
    sig, det, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if t in ("#+", "#-"):
            j = i + 1
            while j < len(toks) and len(toks[j]) == 1 and toks[j] in HEXV:
                j += 1
            v = int("".join(toks[i + 1:j]) or "0", 16) * (-1 if t == "#-" else 1)
            i = j + 1                                   # skip "#."
            if i < len(toks) and toks[i] == "(":
                sig.append("M"); det.append(("M", v, toks[i + 1])); i += 3
            else:
                sig.append("I"); det.append(("I", v, None))
            continue
        if t == "(":
            sig.append("M"); det.append(("M", 0, toks[i + 1])); i += 3; continue
        kind = ("X" if re.fullmatch(r"x\d+", t) else "F" if re.fullmatch(r"f\d+", t) else "C" if t.startswith("csr:")
                else "R" if t.startswith("rm:") else "A" if t.startswith("arg:") else "L" if t.startswith("lbl:") else "?")
        sig.append(kind); det.append((kind, None, t)); i += 1
    return tuple(sig), det


def build(n_per: int = 1000, seed: int = 11) -> dict:
    tk = tokenizer()
    sigs = collections.defaultdict(set)
    rng_ = collections.defaultdict(lambda: [math.inf, -math.inf, 0])
    bases = collections.defaultdict(set)
    regs = collections.defaultdict(set)
    rng = random.Random(seed)
    for core in CORES:
        for prof in PROFILES:
            g = group(prof)
            for _ in range(n_per):
                for line in body_of(core, prof, rng.getrandbits(48), rng.choice([20, 40, 60])):
                    toks = tk.encode_line(line)
                    while toks and toks[0] == "@def":
                        toks = toks[2:]
                    if not toks:
                        continue
                    mn = toks[0]
                    sig, det = _types(tk, toks[1:])
                    if "?" in sig:
                        continue
                    sigs[f"{g}|{mn}"].add(sig)
                    for k, (kind, v, reg) in enumerate(det):
                        if kind in ("I", "M"):
                            r = rng_[f"{g}|{mn}|{k}"]
                            r[0], r[1], r[2] = min(r[0], v), max(r[1], v), math.gcd(r[2], abs(v))
                        if kind == "M":
                            bases[f"{g}|{mn}|{k}"].add(reg)
                        if kind in ("X", "F"):
                            regs[f"{g}|{mn}|{k}"].add(reg)
    import rv64fuzz
    region = rv64fuzz.DATA_DWORDS * 8
    size = {"b": 1, "h": 2, "w": 4, "d": 8}
    for key, r in rng_.items():
        mn = key.split("|")[1]
        if bases.get(key) == {"x30"} and r[0] >= 0:      # offsets into the data region: the whole region, same alignment
            sz = size.get(mn.rstrip("u")[-1], 1) if mn.lstrip("c.f")[:1] in ("l", "s") else 1
            r[0], r[1] = 0, max(r[1], region - sz)
        if mn in LEGAL and r[1] - r[0] > 0:               # an immediate that the generator draws at random
            r[0], r[1], r[2] = min(r[0], LEGAL[mn][0]), max(r[1], LEGAL[mn][1]), 1
    return {"signatures": {k: sorted(map(list, v)) for k, v in sigs.items()},
            "ranges": {k: v for k, v in rng_.items()}, "mem_bases": {k: sorted(v) for k, v in bases.items()},
            "regs": {k: sorted(v) for k, v in regs.items()}}


class Constraint:
    """Per-sequence state machine. allowed() -> set of token ids; step(token id) advances."""

    def __init__(self, gram: dict, tk, profile: str):
        self.g, self.tk, self.grp = gram, tk, group(profile)
        self.reserved = {"x30", "x31"} | ({"x29"} if self.grp != "base" else set())
        v = tk.vocab
        self.ids = {t: i for i, t in enumerate(v)}
        cls = collections.defaultdict(list)
        for i, t in enumerate(v):
            if re.fullmatch(r"x\d+", t): cls["X"].append(t)
            elif re.fullmatch(r"f\d+", t): cls["F"].append(t)
            elif t.startswith("csr:"): cls["C"].append(t)
            elif t.startswith("rm:"): cls["R"].append(t)
            elif t.startswith("arg:"): cls["A"].append(t)
            elif t.startswith("lbl:") and not t[4].isdigit(): cls["L"].append(t)   # no numeric local labels (1f/1b)
        self.cls = cls
        self.mn = {k.split("|", 1)[1]: [tuple(s) for s in v] for k, v in gram["signatures"].items()
                   if k.startswith(self.grp + "|") and k.split("|", 1)[1] not in EXCLUDED}
        # observed label names per group (refs and defs are drawn from these)
        self.defined, self.referenced = set(), set()
        # assembler option state: compressed code is on from the start only in ext (the harness writes
        # `.option rvc` before an ext body); `.option push/pop` nest, `.option rvc/norvc` switch
        self.rvc, self.opt_stack = profile == "ext", []
        self.lines = 0                                   # completed lines: an empty body is never allowed
        self._new_line()

    def _new_line(self):
        self.state, self.cur, self.cfgs, self.after_def = "start", None, [], False

    # configs: (sig, k, sub, acc) with sub in {"op", "num", "num_d", "mem_open", "mem_reg", "mem_close"}
    def _first(self, sig, k):
        """Token set that can start operand k of sig, plus the config states they lead to."""
        out = {}
        t = sig[k]
        if t in ("X", "F", "C", "R", "A"):
            seen = (self.g.get("regs", {}).get(f"{self.grp}|{self.cur}|{k}") if t in ("X", "F") and self.cur.startswith("c.")
                    else None)                           # compressed encodings restrict registers (often x8-x15)
            for tok in (seen if seen else self.cls[t]):     # registers seen at this position (RVC: x8-x15 only)
                if t == "A" and self.cur == ".option" and tok == "arg:pop" and not self.opt_stack:
                    continue                                 # pop needs a matching push
                if t == "X" and k == 0 and self.cur not in WRITER_EXCEPT and not self.cur.startswith(("b", ".")) \
                        and tok in self.reserved:
                    continue
                out.setdefault(tok, []).append((sig, k + 1, "op", None))
        elif t == "L":
            for tok in self.cls["L"]:
                name = tok[4:]
                if name in self.defined and self.cur != "la":
                    continue
                out.setdefault(tok, []).append((sig, k + 1, "op", None))
        elif t in ("I", "M"):
            r = self._range(k)
            lo, hi = (r[0], r[1]) if r else (-2048, 2047)
            for sgn, ok in (("#+", hi >= 0), ("#-", lo < 0)):
                if ok:
                    out.setdefault(sgn, []).append((sig, k, "num", (sgn, "")))
            if t == "M":
                out.setdefault("(", []).append((sig, k, "mem_reg", None))
        return out

    @staticmethod
    def _valid(mag: int, neg: bool, lo, hi, gcd) -> bool:
        v = -mag if neg else mag
        return lo <= v <= hi and (gcd in (0, 1) or v % gcd == 0) and not (gcd == 0 and v != 0)

    @staticmethod
    def _feasible(prefix: str, neg: bool, lo, hi, gcd) -> bool:
        """Is there a valid value whose hex magnitude starts with `prefix`? (lookahead, so decoding never dead-ends)"""
        p = int(prefix, 16)
        lo_m, hi_m = (max(0, -hi), -lo) if neg else (max(0, lo), hi)    # magnitude bounds
        if hi_m < lo_m:
            return False
        g = 1 if gcd in (0, 1) else gcd
        for j in range(0, 17 - len(prefix)):
            a, b = p * 16 ** j, p * 16 ** j + 16 ** j - 1 if j else p
            x, y = max(a, lo_m), min(b, hi_m)
            if x <= y and -(-x // g) * g <= y:
                return True
            if a > hi_m:
                break
        return False

    def _range(self, k):
        return self.g["ranges"].get(f"{self.grp}|{self.cur}|{k}")

    def allowed(self) -> set[int]:
        a = set()
        if self.state == "start":
            a |= {self.ids[m] for m in self.mn if m in self.ids and (self.rvc or not m.startswith("c."))}
            if self.after_def:
                a.add(self.ids["<nl>"])                  # a label alone on its line
            a.add(self.ids["@def"])
            if self.lines and self.referenced <= self.defined and not self.opt_stack:   # non-empty, refs resolved, pushes popped
                a.add(self.ids["<eos>"])
            return a
        if self.state == "def":
            return {self.ids[t] for t in self.cls["L"] if t[4:] not in self.defined and t[4:] in self.referenced} \
                or {self.ids[t] for t in self.cls["L"] if t[4:] not in self.defined}
        for sig, k, sub, acc in self.cfgs:
            if sub == "op":
                if k == len(sig):
                    a.add(self.ids["<nl>"])
                else:
                    a |= {self.ids[t] for t in self._first(sig, k)}
            elif sub == "num":
                sgn, digits = acc
                r = self._range(k)
                lo, hi, gcd = r if r else (-2048, 2047, 1)
                neg = sgn == "#-"
                if digits and self._valid(int(digits, 16), neg, lo, hi, gcd):
                    a.add(self.ids["#."])
                if len(digits) < 16 and digits != "0":          # no leading zeros (as the encoder writes them)
                    for h in "0123456789abcdef":
                        if (digits or h != "0" or self._valid(0, neg, lo, hi, gcd)) and                                 self._feasible(digits + h, neg, lo, hi, gcd):
                            a.add(self.ids[h])
            elif sub == "mem_open":
                a.add(self.ids["("])
            elif sub == "mem_reg":
                bases = self.g["mem_bases"].get(f"{self.grp}|{self.cur}|{k}", ["x30"])
                a |= {self.ids[b] for b in bases if b in self.ids}
            elif sub == "mem_close":
                a.add(self.ids[")"])
        return a or {self.ids["<nl>"]}                  # never a dead end

    def step(self, tid: int) -> None:
        t = self.tk.vocab[tid]
        if t == "<nl>":
            self.lines += 1; self._new_line(); return
        if self.state == "start":
            if t == "@def":
                self.state = "def"; return
            self.state, self.cur = "ops", t
            self.cfgs = [(s, 0, "op", None) for s in self.mn.get(t, [])]
            return
        if self.state == "def":
            self.defined.add(t[4:]); self.state = "start"; self.after_def = True; return   # an instruction may follow
        if self.cur == ".option" and t.startswith("arg:"):
            o = t[4:]
            if o == "push": self.opt_stack.append(self.rvc)
            elif o == "pop" and self.opt_stack: self.rvc = self.opt_stack.pop()
            elif o == "rvc": self.rvc = True
            elif o == "norvc": self.rvc = False
        nxt = []
        for sig, k, sub, acc in self.cfgs:
            if sub == "op" and k < len(sig):
                for cfg in self._first(sig, k).get(t, []):
                    nxt.append(cfg)
                    if sig[k] == "L":
                        self.referenced.add(t[4:])
            elif sub == "num":
                sgn, digits = acc
                if t == "#.":
                    nxt.append((sig, k, "mem_open", None) if sig[k] == "M" else (sig, k + 1, "op", None))
                elif len(t) == 1 and t in HEXV:
                    nxt.append((sig, k, "num", (sgn, digits + t)))
            elif sub == "mem_open" and t == "(":
                nxt.append((sig, k, "mem_reg", None))
            elif sub == "mem_reg":
                nxt.append((sig, k, "mem_close", None))
            elif sub == "mem_close" and t == ")":
                nxt.append((sig, k + 1, "op", None))
        self.cfgs = nxt


def load(path: Path | None = None) -> dict:
    p = Path(path) if path else Path(__file__).with_name("grammar.json")
    if not p.exists():
        p.write_text(json.dumps(build()))
    return json.loads(p.read_text())


if __name__ == "__main__":
    g = build()
    Path(__file__).with_name("grammar.json").write_text(json.dumps(g))
    print(len(g["signatures"]), "mnemonic/group entries,", len(g["ranges"]), "immediate fields")
