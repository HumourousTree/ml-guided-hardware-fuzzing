#!/usr/bin/env python3
"""Unified hardware/software co-coverage C_U for benchmark runs saved with --save-programs.

  python3 tools/unified_coverage.py logs/bench/unified --out logs/experiments/unified_coverage.json

    C_U = w_L C_line + w_T C_toggle + w_S C_ISS + w_F C_func,     sum w = 1   (default: equal weights)

  C_line, C_toggle  cumulative Verilator line / toggle coverage of the DUT (from the run's series)
  C_ISS             statement coverage of the reference model's executable lines (sw_reference/golden_rv32i.py,
                    the Python analogue of gcov on a C++ ISS), measured with a sys.settrace line tracer
  C_func            mean coverage of five functional covergroups sampled from the reference trace:
                      G1 mnemonics (48 RV32IM + FENCE), G2 branch type x {taken, not taken} (12),
                      G3 load/store size x byte offset, aligned only (20), G4 RV32M corner cases (div/rem by zero,
                      signed overflow, MULH* with negative operands; 12), G5 register-value patterns (7)
The campaign's legacy 8-category/7-pattern functional metric saturates at 100% within a few programs, so it is not used.
"""
import argparse
import ast
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sw_reference.golden_rv32i as ref  # noqa: E402
from harness.riscv_cosim import analyze_program  # noqa: E402

R_MN = {(0, 0): "add", (0x20, 0): "sub", (0, 1): "sll", (0, 2): "slt", (0, 3): "sltu", (0, 4): "xor", (0, 5): "srl",
        (0x20, 5): "sra", (0, 6): "or", (0, 7): "and", (1, 0): "mul", (1, 1): "mulh", (1, 2): "mulhsu", (1, 3): "mulhu",
        (1, 4): "div", (1, 5): "divu", (1, 6): "rem", (1, 7): "remu"}
I_MN = {0: "addi", 2: "slti", 3: "sltiu", 4: "xori", 6: "ori", 7: "andi", 1: "slli", 5: "srli"}
L_MN = {0: "lb", 1: "lh", 2: "lw", 4: "lbu", 5: "lhu"}
S_MN = {0: "sb", 1: "sh", 2: "sw"}
B_MN = {0: "beq", 1: "bne", 4: "blt", 5: "bge", 6: "bltu", 7: "bgeu"}
MNEMONICS = sorted(set(R_MN.values()) | set(I_MN.values()) | {"srai"} | set(L_MN.values()) | set(S_MN.values())
                   | set(B_MN.values()) | {"lui", "auipc", "jal", "jalr", "fence"})
BRANCH_BINS = [f"{b}:{t}" for b in B_MN.values() for t in ("T", "N")]
MEM_BINS = ([f"{m}@{o}" for m in ("lb", "lbu", "sb") for o in range(4)] + [f"{m}@{o}" for m in ("lh", "lhu", "sh") for o in (0, 2)]
            + ["lw@0", "sw@0"])
M_BINS = [f"{op}:{c}" for op in ("div", "divu", "rem", "remu") for c in ("zero",)] + \
         ["div:ovf", "rem:ovf"] + [f"{op}:{s}" for op in ("mulh", "mulhsu") for s in ("neg_a", "neg_b")] + ["mulhu:big", "mul:neg"]
PATTERNS = ref.CoverageTracker.REGISTER_PATTERNS
GROUPS = {"G1_mnemonic": MNEMONICS, "G2_branch": BRANCH_BINS, "G3_memory": MEM_BINS, "G4_mext": M_BINS, "G5_patterns": PATTERNS}


def mnemonic(w: int) -> str | None:
    op, f3, f7 = w & 0x7F, (w >> 12) & 7, (w >> 25) & 0x7F
    if op == 0x33: return R_MN.get((f7, f3))
    if op == 0x13: return "srai" if f3 == 5 and f7 == 0x20 else I_MN.get(f3)
    if op == 0x03: return L_MN.get(f3)
    if op == 0x23: return S_MN.get(f3)
    if op == 0x63: return B_MN.get(f3)
    return {0x37: "lui", 0x17: "auipc", 0x6F: "jal", 0x67: "jalr", 0x0F: "fence"}.get(op)


def pattern(v: int) -> str:
    v &= 0xFFFFFFFF
    if v == 0: return "all_zeros"
    if v == 0xFFFFFFFF: return "all_ones"
    if v in (0x55555555, 0xAAAAAAAA): return "alternating_bits"
    if v in (0x7FFFFFFF, 0x80000000, 1, 0xFFFFFFFE): return "boundary_extremes"
    if bin(v).count("1") == 1: return "walking_ones"
    if bin(v).count("0") == 1: return "walking_zeros"
    return "arbitrary_data"


class FunctionalCoverage:
    def __init__(self):
        self.hit = {g: set() for g in GROUPS}
        self.mix: dict[str, int] = {}

    def sample(self, w: int, a: int, b: int, rec: dict) -> None:
        mn = mnemonic(w)
        if mn is None:
            return
        self.hit["G1_mnemonic"].add(mn)
        self.mix[mn] = self.mix.get(mn, 0) + 1
        if mn in B_MN.values():
            self.hit["G2_branch"].add(f"{mn}:{'T' if rec['pc_wdata'] != (rec['pc_rdata'] + 4) & 0xFFFFFFFF else 'N'}")
        if (w & 0x7F) in (0x03, 0x23):
            b_ = f"{mn}@{rec['mem_addr'] & 3}"
            if b_ in MEM_BINS: self.hit["G3_memory"].add(b_)
        if mn in ("div", "divu", "rem", "remu") and b == 0: self.hit["G4_mext"].add(f"{mn}:zero")
        if mn in ("div", "rem") and a == 0x80000000 and b == 0xFFFFFFFF: self.hit["G4_mext"].add(f"{mn}:ovf")
        if mn in ("mulh", "mulhsu") and a >> 31: self.hit["G4_mext"].add(f"{mn}:neg_a")
        if mn in ("mulh", "mulhsu") and b >> 31: self.hit["G4_mext"].add(f"{mn}:neg_b")
        if mn == "mulhu" and a >> 31 and b >> 31: self.hit["G4_mext"].add("mulhu:big")
        if mn == "mul" and (a >> 31) != (b >> 31): self.hit["G4_mext"].add("mul:neg")
        if rec["rd_addr"]:
            self.hit["G5_patterns"].add(pattern(rec["rd_wdata"]))

    def summary(self) -> dict:
        per = {g: round(100.0 * len(self.hit[g]) / len(bins), 2) for g, bins in GROUPS.items()}
        return {"groups": per, "C_func": round(sum(per.values()) / len(per), 2),
                "missing": {g: sorted(set(bins) - self.hit[g]) for g, bins in GROUPS.items()}}


def iss_executable_lines() -> set[int]:
    """Executable statement lines of RV32IReference (the statement-coverage denominator)."""
    src = inspect.getsource(ref.RV32IReference)
    first = inspect.getsourcelines(ref.RV32IReference)[1]
    lines = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.FunctionDef, ast.ClassDef)) and \
                not (isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)):
            lines.add(first + node.lineno - 1)
    return lines


class StatementTracer:
    def __init__(self):
        self.file = str(Path(ref.__file__).resolve())
        self.lines: set[int] = set()

    def __enter__(self):
        def local(frame, event, arg):
            if event == "line":
                self.lines.add(frame.f_lineno)
            return local

        def glob(frame, event, arg):
            return local if frame.f_code.co_filename == self.file else None
        sys.settrace(glob)
        return self

    def __exit__(self, *exc):
        sys.settrace(None)


def run_program(words: list[int], fc: FunctionalCoverage, steps: int) -> None:
    iss = ref.RV32IReference(words)
    for _ in range(steps):
        w = iss.peek_insn()
        a, b = iss.regs[(w >> 15) & 31], iss.regs[(w >> 20) & 31]
        rec = iss.step()
        fc.sample(w, a, b, rec)


def analyse_run(run_json: Path, programs: Path, weights: dict, checkpoints: list[int]) -> dict:
    run = json.loads(run_json.read_text())
    up = run.get("universe_pages") or {}
    fc = FunctionalCoverage()
    denom = iss_executable_lines()
    traj, tracer = [], StatementTracer()
    series = {s["n_programs"]: s for s in run["series"]}
    with tracer:
        for line in programs.read_text().splitlines():
            p = json.loads(line)
            words = [int(x, 0) for x in p["words"]]
            info = analyze_program(words)
            run_program(words, fc, info["dynamic_steps"] if info["ok"] else 0)
            n = p["n"]
            if n in checkpoints or n == len(run["series"]):
                s = series.get(n, run["series"][-1])
                comp = {"C_line": 100.0 * s.get("pages", {}).get("v_line", 0) / max(1, up.get("v_line", 1)),
                        "C_toggle": 100.0 * s.get("pages", {}).get("v_toggle", 0) / max(1, up.get("v_toggle", 1)),
                        "C_ISS": 100.0 * len(tracer.lines & denom) / len(denom), "C_func": fc.summary()["C_func"]}
                comp = {k: round(v, 2) for k, v in comp.items()}
                comp["C_U"] = round(sum(weights[k] * comp[k] for k in weights), 2)
                traj.append(dict(n_programs=n, **comp))
    final = traj[-1] if traj else {}
    total = sum(fc.mix.values()) or 1
    return {"method": run["method"], "seed": run["seed"], "programs": len(run["series"]), "final": final, "trajectory": traj,
            "functional": fc.summary(), "mnemonic_mix_pct": {k: round(100.0 * v / total, 2) for k, v in sorted(fc.mix.items())},
            "iss_unexecuted_lines": sorted(denom - tracer.lines)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench_dir")
    ap.add_argument("--out", default="logs/experiments/unified_coverage.json")
    ap.add_argument("--weights", default="0.25,0.25,0.25,0.25", help="w_line,w_toggle,w_iss,w_func")
    a = ap.parse_args()
    w = dict(zip(("C_line", "C_toggle", "C_ISS", "C_func"), map(float, a.weights.split(","))))
    assert abs(sum(w.values()) - 1) < 1e-9, "weights must sum to 1"
    cps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 3000, 5000, 10000]
    res = []
    for rj in sorted(Path(a.bench_dir).glob("*_seed*.json")):
        pj = rj.with_suffix(".programs.jsonl")
        if pj.exists():
            r = analyse_run(rj, pj, w, cps)
            res.append(r)
            print(f"{r['method']:12s} seed {r['seed']}: {r['final']}", flush=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"weights": w, "groups": {g: len(b) for g, b in GROUPS.items()},
                                       "iss_statements": len(iss_executable_lines()), "runs": res}, indent=2))


if __name__ == "__main__":
    main()
