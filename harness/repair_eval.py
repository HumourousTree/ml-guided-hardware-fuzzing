"""Guarded LLM repair evaluation with independent samples (pass@k) for each injected bug.

pass@k uses the unbiased estimator of Chen et al. (2021): 1 - C(n-c, k)/C(n, k) from n independent samples with c successes.
Each sample starts from a FRESH model session and a cleared rejection memory, and uses `max_retries` feedback attempts
(default 1 = no feedback) so that samples are independent.  A sample is a success iff
  (1) the candidate passes Verilator lint, the minimal reproducer and the grow-verification prefixes (inside the repair call), and
  (2) the *whole regression suite* (seed programs + the bug's directed stimulus) matches lockstep on the candidate RTL.
`exact_fix` additionally records whether the candidate is textually identical to the golden RTL.
`feedback_success` reports the full guarded loop (lint retries, up to 3 attempts with feedback) as a separate metric.
"""
import argparse
import json
import math
import shutil
import time
from pathlib import Path

from hw_design.bug_injector import load_manifest, inject_bug, restore_golden, MUTABLE_RTL, GOLDEN_RTL
from harness import pico_campaign as pc
from harness.riscv_cosim import (build_and_simulate_program, compare_rvfi_trace, get_space_safe_tempdir, prepare_simulator)


def pass_at_k(n: int, c: int, k: int) -> float:
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def _suite_ok(rtl: Path, programs: list[dict], workdir: Path) -> bool:
    for prog in programs:
        sim = build_and_simulate_program(rtl, prog, workdir, generate_vcd=False)
        if not sim["ok"] or not compare_rvfi_trace(prog, sim["hardware_trace"], hardware_trap=sim.get("hardware_trap", False))["ok"]:
            return False
    return True


def evaluate_bug(bug_id: str, samples: int = 5, suite: str = "all", feedback_attempts: int = 3) -> dict:
    manifest = load_manifest(suite)
    stim = pc._generate_bug_targeted_stimulus(bug_id)
    work = get_space_safe_tempdir(f"repair-{bug_id}-")
    try:
        if not inject_bug(bug_id, suite):
            return {"bug_id": bug_id, "error": "injection failed"}
        ok, log = prepare_simulator(MUTABLE_RTL, work)
        if not ok:
            return {"bug_id": bug_id, "error": "RTL does not build: " + log[-300:]}
        sim = build_and_simulate_program(MUTABLE_RTL, stim, work, generate_vcd=False)
        cmp = compare_rvfi_trace(stim, sim.get("hardware_trace", []), hardware_trap=sim.get("hardware_trap", False))
        if not cmp["mismatches"]:
            return {"bug_id": bug_id, "detected": False}
        regression = list(pc.DEFAULT_SEED_PROGRAMS) + [stim]
        golden = GOLDEN_RTL.read_text()
        cand_dir = work / "verify"; cand_dir.mkdir()

        def one(max_retries: int) -> dict:
            pc._GEMINI_CHATS.clear(); pc._GEMINI_TURNS.clear(); pc._REJECTED_NOTES.clear()      # independence
            t0 = time.time()
            cand, src, attempts, info = pc._llm_repair_hardware_with_delta_debug({"mem_delay": 0}, stim, cmp, work, max_retries=max_retries)
            rec = {"source": src, "attempts": attempts, "seconds": round(time.time() - t0, 1), "localized": info.get("localized_subsystem"),
                   "status": info.get("status"), "success": False, "exact_fix": False}
            if cand and src.startswith("gemini"):
                f = cand_dir / "candidate.sv"; f.write_text(cand)
                rec["success"] = _suite_ok(f, regression, cand_dir)
                rec["exact_fix"] = rec["success"] and cand.strip() == golden.strip()
                (cand_dir / "build_stamp_reset").write_text("")
                (cand_dir / ".build_stamp").unlink(missing_ok=True)                   # force rebuild for the next candidate
            return rec

        indep = [one(1) for _ in range(samples)]
        c = sum(r["success"] for r in indep)
        fb = one(feedback_attempts)
        return {"bug_id": bug_id, "level": manifest[bug_id]["level"], "detected": True, "samples": samples, "successes": c,
                "pass@1": round(pass_at_k(samples, c, 1), 3), "pass@5": round(pass_at_k(samples, c, min(5, samples)), 3) if samples >= 5 else None,
                "exact_fix_rate": round(sum(r["exact_fix"] for r in indep) / samples, 3), "independent": indep,
                "feedback_success": fb["success"], "feedback_attempts_used": fb["attempts"], "feedback_run": fb}
    finally:
        restore_golden()
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="pass@k evaluation of guarded LLM repair")
    ap.add_argument("--suite", choices=["legacy", "v2", "all"], default="all")
    ap.add_argument("--bugs", nargs="*")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--out", default="logs/experiments/repair_eval.json")
    a = ap.parse_args()
    bugs = a.bugs or list(load_manifest(a.suite))
    results = {}
    for b in bugs:
        results[b] = evaluate_bug(b, a.samples, a.suite)
        r = results[b]; print(f"{b:28s} " + (f"pass@1={r['pass@1']} pass@5={r['pass@5']} exact={r['exact_fix_rate']} feedback={r['feedback_success']}" if r.get("detected") else str(r)))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True); Path(a.out).write_text(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
