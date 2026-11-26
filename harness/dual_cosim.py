"""Dual-hart PicoRV32 co-simulation and grant-log-ordered reference (docs/DUAL_HART.md, Track 2).

The system has no caches and one arbitrated bus, so it is sequentially consistent and the arbiter's grant log is the total
order of all memory transactions. The reference runs one RV32IM model per hart over ONE shared memory and performs each
hart's data access exactly when its grant appears in the log; everything between two accesses of a hart touches only
that hart's registers, so it can run eagerly. The result is the exact architectural trace both harts must produce for
that interleaving, which is checked retirement by retirement against both RVFI streams (the single-core rules: same
fields, no missing or extra retirements). The log itself is also checked against the ISA:
  * every data grant must belong to the hart's next memory instruction, with that instruction's address and strobes,
  * an amoswap.w is one read grant immediately followed by the write grant of the same AMO unit (atomicity),
  * a hart that has not finished must still be making progress when the run ends (no starvation / deadlock).
Replaying the recorded requester ids of the data grants (``run(..., replay=ids)``) forces the arbiter to perform loads,
stores and AMOs in the same order (fetches stay free), so a failing interleaving reproduces under any memory timing and
stays deterministic while the program is minimised.

Memory map: hart 0 program at 0x0000, hart 1 at 0x4000 (16 KiB each, private), shared data 0x8000-0xFFFF.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from sw_reference.golden_rv32i import RV32IReference, u32

ROOT = Path(__file__).resolve().parents[1]
RTL = [ROOT / "hw_design/dual/dual_top.v", ROOT / "hw_design/dual/arbiter_rr.v", ROOT / "hw_design/dual/amo_pcpi.v"]
CORE = ROOT / "hw_design/upstream_picorv32.v"         # pristine upstream core (picorv32.v is the campaign mirror)
TB = ROOT / "hw_design/dual/dual_tb.cpp"
PROG_BASE = (0x0000, 0x4000)
SHARED = (0x8000, 0x10000)
PROGRESS = 2000                      # cycles: a hart that retired this recently at a timeout was still progressing
FIELDS = ("pc_rdata", "pc_wdata", "insn", "rd_addr", "rd_wdata", "mem_addr", "mem_rmask", "mem_wmask")


def is_amoswap(insn: int) -> bool:
    return insn & 0x7F == 0x2F and (insn >> 12) & 7 == 2 and insn >> 27 == 0x01


def build(workdir: Path, core_rtl: Path = CORE, extra_rtl: list[Path] | None = None) -> Path:
    """Verilate dual_top (+ the given core RTL); reuses the binary while sources are unchanged."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    srcs = [*(extra_rtl or RTL), Path(core_rtl), TB]
    stamp = hashlib.sha256(b"".join(p.read_bytes() for p in srcs)).hexdigest()
    binary = workdir / "obj_dir" / "sim_dual"
    if binary.exists() and (workdir / ".stamp").exists() and (workdir / ".stamp").read_text() == stamp:
        return binary
    shutil.rmtree(workdir / "obj_dir", ignore_errors=True)
    names = []                                       # copied in: make cannot handle spaces in source paths
    for i, p in enumerate(srcs):
        names.append(f"src{i}_{p.name}")
        shutil.copyfile(p, workdir / names[-1])
    cmd = ["verilator", "-Wno-fatal", "-Wno-lint", "-Wno-style", "-DRISCV_FORMAL", "--top-module", "dual_top", "--cc",
           *names[:-1], "--exe", names[-1], "-O2", "-o", "sim_dual", "--Mdir", "obj_dir"]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir)
    if res.returncode:
        raise RuntimeError("verilator failed:\n" + res.stderr[-3000:])
    res = subprocess.run(["make", "-C", "obj_dir", "-f", "Vdual_top.mk", "-j4"], capture_output=True, text=True, cwd=workdir)
    if res.returncode or not binary.exists():
        raise RuntimeError("make failed:\n" + res.stderr[-3000:])
    (workdir / ".stamp").write_text(stamp)
    return binary


def run(binary: Path, words0: list[int], words1: list[int], workdir: Path, max_cycles: int = 200_000,
        mem_delay: int = 0, seed: int = 1, replay: list[int] | None = None, tag: str = "run") -> dict:
    workdir = Path(workdir)
    for h, words in enumerate((words0, words1)):
        if len(words) * 4 > 0x4000:
            raise ValueError(f"hart {h} program exceeds 16 KiB")
        (workdir / f"{tag}_p{h}.hex").write_text("".join(f"{w & 0xFFFFFFFF:08x}\n" for w in words))
    args = [str(binary), str(workdir / f"{tag}_p0.hex"), str(workdir / f"{tag}_p1.hex"), str(max_cycles),
            str(mem_delay), str(seed)]
    if replay is not None:
        (workdir / f"{tag}_replay.txt").write_text("".join(f"{g}\n" for g in replay))
        args.append(str(workdir / f"{tag}_replay.txt"))
    out = subprocess.run(args, capture_output=True, text=True, timeout=600).stdout
    trace, grants, halts, end = ([], []), [], {}, {}
    for line in out.splitlines():
        rec = json.loads(line)
        if "h" in rec: trace[rec["h"]].append(rec)
        elif "g" in rec: grants.append(rec)
        elif "halt" in rec: halts[rec["halt"]] = rec["cycle"]
        elif "end" in rec: end = rec
    return {"trace": trace, "grants": grants, "halts": halts, "end": end.get("end"), "cycles": end.get("cycles"),
            "replay_skips": end.get("replay_skips", 0), "replay_abandoned": end.get("replay_abandoned", -1)}


class _Hart:
    def __init__(self, h: int, words: list[int], mem: dict):
        self.h, self.base, self.end = h, PROG_BASE[h], PROG_BASE[h] + 4 * len(words)
        self.m = RV32IReference([], self.base)
        self.m.mem = mem                             # shared memory (program regions are disjoint)
        for i, w in enumerate(words):
            self.m._store_raw(self.base + 4 * i, 4, w)
        self.done = False
        self.out: list[dict] = []
        self.limit = None                            # max retirements to model (a hart that loops without memory
        self.runaway = False                         # accesses would otherwise be stepped forever)

    def running(self) -> bool:
        if self.limit is not None and len(self.out) >= self.limit:
            self.runaway = True
            return False
        return not self.done and self.base <= self.m.pc < self.end

    def next_access(self):
        """Run non-memory instructions; return ("load"|"store"|"amo", word address, wstrb) of the next access, or None."""
        while self.running():
            insn = self.m.peek_insn()
            op = insn & 0x7F
            a = self.m.regs[(insn >> 15) & 31]
            if op == 0x03:
                return "load", u32(a + _sext(insn >> 20, 12)) & ~3, 0
            if op == 0x23:
                addr = u32(a + _sext(((insn >> 25) << 5) | ((insn >> 7) & 31), 12))
                size = {0: 1, 1: 2, 2: 4}.get((insn >> 12) & 7, 4)
                return "store", addr & ~3, ((1 << size) - 1) << (addr & 3)
            if is_amoswap(insn):
                return "amo", a & ~3, 0xF
            try:
                self.out.append(self.m.step())
            except ValueError:
                self.done = True
        return None

    def do_access(self) -> None:
        insn = self.m.peek_insn()
        if is_amoswap(insn):
            rd, rs1, rs2 = (insn >> 7) & 31, (insn >> 15) & 31, (insn >> 20) & 31
            addr, new = self.m.regs[rs1] & ~3, self.m.regs[rs2]
            old = self.m._load_raw(addr, 4)
            self.m._store_raw(addr, 4, new)
            if rd:
                self.m.regs[rd] = old
            pc = self.m.pc
            self.m.pc = u32(pc + 4)
            self.out.append({"pc_rdata": pc, "pc_wdata": self.m.pc, "insn": insn, "rd_addr": rd, "rd_wdata": old if rd else 0,
                             "mem_addr": 0, "mem_rmask": 0, "mem_wmask": 0, "mem_rdata": 0, "mem_wdata": 0})
        else:
            self.out.append(self.m.step())


def _sext(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def reference(words0: list[int], words1: list[int], grants: list[dict], limits=None, truncated: bool = False) -> dict:
    """Replay both harts over one memory in grant order. Returns expected traces and any log violations.
    `limits` caps the retirements modelled per hart (the comparison needs at most one more than the DUT retired);
    `truncated`: the run hit the cycle limit, so an AMO read may be the last logged transaction (its write is cut off)."""
    mem: dict = {}
    harts = [_Hart(0, words0, mem), _Hart(1, words1, mem)]
    for h, lim in enumerate(limits or (None, None)):
        harts[h].limit = lim
    violations = []
    data = [g for g in grants if not g["i"]]
    k = 0
    while k < len(data):
        g = data[k]
        h = g["g"] % 2
        hart = harts[h]
        acc = hart.next_access()
        where = f"grant {k} (id {g['g']}, addr 0x{g['a']:x}, cycle {g['cycle']})"
        if acc is None:
            violations.append({"kind": "unexpected_grant", "at": where, "error": f"hart {h} has no memory access left"})
            break
        kind, addr, wstrb = acc
        want_id = h + 2 if kind == "amo" else h
        if g["g"] != want_id or (g["a"] & ~3) != addr:
            violations.append({"kind": "grant_mismatch", "at": where,
                               "error": f"hart {h} next access is {kind} 0x{addr:x} by requester {want_id}"})
            break
        if kind == "amo":
            nxt = data[k + 1] if k + 1 < len(data) else None
            if nxt is not None and nxt["g"] == want_id and (nxt["a"] & ~3) == addr and not nxt["w"]:
                violations.append({"kind": "amo_write_lost", "at": where, "error": "AMO write went out without byte strobes"})
                break
            if nxt is None and truncated:
                break                                # cut off by the cycle limit between the AMO's read and write
            if nxt is None or nxt["g"] != want_id or not nxt["w"] or (nxt["a"] & ~3) != addr:
                violations.append({"kind": "amo_not_atomic", "at": where,
                                   "error": f"AMO read not immediately followed by its write (next: {nxt})"})
                break
            k += 2
        else:
            if bool(g["w"]) != (kind == "store") or (kind == "store" and g["w"] != wstrb):
                violations.append({"kind": "grant_mismatch", "at": where,
                                   "error": f"{kind} with wstrb {wstrb:#x} logged as wstrb {g['w']:#x}"})
                break
            k += 1
        hart.do_access()
    for hart in harts:                               # finish: remaining non-memory work (a remaining access has no grant)
        acc = hart.next_access()
        hart.pending = acc
    return {"expected": [harts[0].out, harts[1].out], "pending": [harts[0].pending, harts[1].pending],
            "runaway": [harts[0].runaway, harts[1].runaway],
            "violations": violations, "final_mem": {a: v for a, v in mem.items() if SHARED[0] <= a < SHARED[1]}}


def check(words0: list[int], words1: list[int], result: dict, max_report: int = 4) -> dict:
    """Verdict for one run: per-hart lockstep comparison against the grant-ordered reference."""
    timeout = result["end"] == "timeout"
    ref = reference(words0, words1, result["grants"], limits=[len(t) + 64 for t in result["trace"]], truncated=timeout)
    mismatches = list(ref["violations"])
    for h in (0, 1):
        exp, act = ref["expected"][h], list(result["trace"][h])
        end_pc = PROG_BASE[h] + 4 * len((words0, words1)[h])
        if act and act[-1].get("trap") and act[-1]["pc_rdata"] == end_pc:
            act.pop()                                # termination: the zero word after the program traps
        # at a timeout, a hart that retired within the last PROGRESS cycles was still making progress: it is a
        # non-terminating program cut off by the limit, not a starved hart (starvation = no progress)
        alive = timeout and bool(act) and result["cycles"] - act[-1]["cycle"] < PROGRESS
        stuck_kind = "program_nonterminating" if alive else ("starved" if timeout else None)
        if ref["runaway"][h] and timeout:
            # the model was still running when it reached the cap: the program itself does not terminate (e.g. a
            # loop whose counter a minimisation step removed). Compare what the DUT retired; if that agrees, the run
            # is reported as a non-terminating program, never as starvation.
            exp = exp[:len(act)]
            if all(not ({f for f in FIELDS if (e[f] & ~3 if f == "mem_addr" else e[f]) != (a[f] & ~3 if f == "mem_addr" else a[f])}
                        or a.get("trap")) for e, a in zip(exp, act)):
                mismatches.append({"kind": "program_nonterminating", "hart": h, "retired": len(act)})
                continue
        for i in range(max(len(exp), len(act))):
            if i >= len(exp):
                mismatches.append({"kind": "extra_retire", "hart": h, "order": i, "actual": act[i]}); break
            if i >= len(act):
                mismatches.append({"kind": stuck_kind or "missing_retire", "hart": h, "order": i, "expected": exp[i]}); break
            e, a = exp[i], act[i]
            diff = {f: (e[f], a[f]) for f in FIELDS
                    if (e[f] & ~3 if f == "mem_addr" else e[f]) != (a[f] & ~3 if f == "mem_addr" else a[f])}
            if a.get("trap"):
                diff["trap"] = (0, 1)
            if diff:
                mismatches.append({"kind": "field_mismatch", "hart": h, "order": i, "pc": e["pc_rdata"], "diff": diff}); break
        if ref["pending"][h] is not None and not any(m.get("hart") == h for m in mismatches):
            mismatches.append({"kind": stuck_kind or "missing_access", "hart": h, "pending": ref["pending"][h]})
    return {"ok": not mismatches, "mismatches": mismatches[:max_report], "retired": [len(t) for t in result["trace"]],
            "grants": len(result["grants"]), "end": result["end"], "cycles": result["cycles"],
            "replay_skips": result.get("replay_skips", 0)}
