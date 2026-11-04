#!/usr/bin/env python3
"""Verify the PicoRV32 FENCE-rd real-bug benchmark entry (hw_design/real_bugs/picorv32_fence_rd).

  python3 tools/check_real_bug.py [--seeds 3] [--programs 3000]

1. reproducer + sweep: FENCE with every rd in x1..x31 and varied reserved fields (rs1, pred/succ, fm); each case writes 123 to rd,
   executes the FENCE, then copies rd.  Expected: every case diverges on upstream master, none on the fixed core.
2. E0 control on the fixed core (must stay 150/150).
3. blind mutation fuzzing of the fixed core with FENCE at 2% per slot (same settings that hit the bug on master): 0 mismatches.
4. Verilator lint of the fixed core.
Writes hw_design/real_bugs/picorv32_fence_rd/verification.json.
"""
import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.pico_campaign import NOP, _i  # noqa: E402
from harness.riscv_cosim import build_and_simulate_program, compare_rvfi_trace, get_space_safe_tempdir, run_verilator_lint  # noqa: E402

ENTRY = ROOT / "hw_design" / "real_bugs" / "picorv32_fence_rd"
UPSTREAM, FIXED = ROOT / "hw_design" / "upstream_picorv32.v", ENTRY / "picorv32_fixed.v"


def fence(rd: int, rs1: int = 0, pred: int = 0xF, succ: int = 0xF, fm: int = 0) -> int:
    return (fm << 28) | (pred << 24) | (succ << 20) | (rs1 << 15) | (rd << 7) | 0x0F


def case_program(rd: int, rs1: int, pred: int, succ: int, fm: int) -> dict:
    dst = 31 if rd != 31 else 30                                   # copy of rd after the FENCE
    words = [_i(123, 0, 0, rd), fence(rd, rs1, pred, succ, fm), _i(0, rd, 0, dst), NOP]
    return {"id": f"fence-rd{rd}-rs{rs1}-p{pred}-s{succ}-fm{fm}", "base": "0x00000000", "words": [f"0x{w:08x}" for w in words]}


def diverges(rtl: Path, prog: dict, work: Path) -> bool:
    sim = build_and_simulate_program(rtl, prog, work, generate_vcd=False)
    return not (sim["ok"] and compare_rvfi_trace(prog, sim["hardware_trace"], hardware_trap=sim.get("hardware_trap", False))["ok"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--programs", type=int, default=3000)
    a = ap.parse_args()
    rng = random.Random(2026)
    cases = [case_program(rd, rng.choice([0, rd, rng.randrange(32)]), rng.randrange(16), rng.randrange(16), rng.choice([0, 8]))
             for rd in range(1, 32)]
    res = {"bug": "FENCE with a non-zero reserved rd field writes an undefined ALU value (0 in Verilator) to rd",
           "upstream_commit": (UPSTREAM.with_suffix(".v.commit").read_text().split()[0]
                               if UPSTREAM.with_suffix(".v.commit").exists() else None)}
    for name, rtl in (("upstream", UPSTREAM), ("fixed", FIXED)):
        work = get_space_safe_tempdir(f"realbug-{name}-")
        res[f"{name}_divergent_cases"] = sum(diverges(rtl, p, work) for p in cases)
        subprocess.run(["rm", "-rf", str(work)])
    res["cases"] = len(cases)
    lint_ok, _ = run_verilator_lint(rtl_path=FIXED, workdir=get_space_safe_tempdir("realbug-lint-"))
    res["fixed_lint_ok"] = lint_ok
    from harness.run_experiments import run_experiment_e0
    e0 = run_experiment_e0(upstream=FIXED, tag="_fence_rd_fixed")
    res["fixed_e0"] = {"matched": e0["runs_matched"], "runs": e0["runs"], "fence_rd_probe_ok": e0["fence_rd_probe"]["ok"],
                       "p6_probe_ok": e0["cascade_p6_fence_probe"]["ok"]}
    out = ROOT / "logs" / "bench" / "fence_rd_fixed"
    subprocess.run([sys.executable, "-m", "harness.benchmark", "--methods", "mutation", "random", "--seeds", str(a.seeds),
                    "--rtl", str(FIXED), "--fence-rate", "0.02", "--stop-on-mismatch", "--budget-programs", str(a.programs),
                    "--budget-seconds", "86400", "--out", str(out)], cwd=ROOT, check=True)
    runs = [json.loads(f.read_text()) for f in sorted(out.glob("*_seed*.json"))]
    res["fixed_fuzz"] = {"runs": len(runs), "programs": sum(r["series"][-1]["n_programs"] for r in runs if r["series"]),
                         "mismatches": sum(1 for r in runs if r.get("bug_detected"))}
    res["verdict"] = ("CONFIRMED: bug present on upstream, removed by the fix without regressions"
                      if res["upstream_divergent_cases"] == len(cases) and res["fixed_divergent_cases"] == 0 and lint_ok
                      and e0["runs_matched"] == e0["runs"] and res["fixed_e0"]["fence_rd_probe_ok"]
                      and res["fixed_fuzz"]["mismatches"] == 0 else "CHECK FAILED")
    (ENTRY / "verification.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
