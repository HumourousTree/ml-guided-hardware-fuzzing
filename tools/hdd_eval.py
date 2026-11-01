#!/usr/bin/env python3
"""Delta-debugging evaluation on *fuzzer-found* failures (not the hand-written stimuli).

  python3 tools/hdd_eval.py logs/bench/bugs --out logs/experiments/hdd_eval.json [--max-per-bug 6]

For every time-to-bug run that found its bug (harness.benchmark --bug ... saves the failing program), re-run the
failure on the buggy RTL, slice it (architectural-state preload), then minimise the slice (or the original program if
the slice loses the failure) with flat ddmin and with hierarchical DD (basic blocks, then instructions).  Records
static length, dynamic retired instructions and cycles before/after, oracle tests, wall time, and 1-minimality.
"""
import argparse
import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.delta_debugger import ddmin_reproducer, hdd_reproducer, is_one_minimal, slice_to_minimal_reproducer  # noqa: E402
from harness.riscv_cosim import (build_and_simulate_program, compare_rvfi_trace, get_space_safe_tempdir,  # noqa: E402
                                 validate_program)
from hw_design.bug_injector import injected_text, load_manifest  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench_dir")
    ap.add_argument("--out", default="logs/experiments/hdd_eval.json")
    ap.add_argument("--max-per-bug", type=int, default=6)
    a = ap.parse_args()
    runs = defaultdict(list)
    for f in sorted(Path(a.bench_dir).glob("*_bug-*_seed*.json")):
        r = json.loads(f.read_text())
        if r.get("bug_detected") and r["bug_detected"].get("words"):
            runs[r["bug"]].append(r)
    manifest = load_manifest("all")
    results = []
    for bug, rs in sorted(runs.items()):
        work = get_space_safe_tempdir(f"hdd-{bug}-")
        rtl = work / "buggy.sv"; rtl.write_text(injected_text([bug]))

        def sim(p):
            s = build_and_simulate_program(rtl, p, work, generate_vcd=False)
            return s, compare_rvfi_trace(p, s.get("hardware_trace", []), hardware_trap=s.get("hardware_trap", False))

        def fails(p):
            s, c = sim(p)
            return bool(s["ok"] and c["mismatches"])

        for r in rs[: a.max_per_bug]:
            prog = {"id": f"{bug}-{r['method']}-s{r['seed']}", "base": "0x00000000", "words": r["bug_detected"]["words"]}
            s0, c0 = sim(prog)
            if not (s0["ok"] and c0["mismatches"]):
                results.append({"bug_id": bug, "method": r["method"], "seed": r["seed"], "error": "failure did not reproduce"}); continue
            t = time.time()
            sliced, K, _ = slice_to_minimal_reproducer(prog, c0, True, lambda p: validate_program(p)["valid"])
            slice_ok = fails(sliced)
            base = sliced if slice_ok else prog
            t_slice = time.time() - t
            rec = {"bug_id": bug, "level": manifest[bug]["level"], "method": r["method"], "seed": r["seed"],
                   "detect_programs": r["bug_detected"]["n_programs"], "detect_seconds": r["bug_detected"]["t"],
                   "original_length": len(prog["words"]), "original_retired": s0["retired"], "original_cycles": s0["cycles"],
                   "failure_index_k": K, "slice_reproduces": slice_ok, "slice_length": len(sliced["words"]),
                   "slice_seconds": round(t_slice, 2), "mismatch_fields": sorted((c0["mismatches"][0].get("differences") or {}).keys()),
                   "mismatch_kind": c0["mismatches"][0].get("kind")}
            for name, fn in (("ddmin", lambda p: ddmin_reproducer(p, fails)), ("hdd", lambda p: hdd_reproducer(p, fails))):
                t = time.time()
                mini, info = fn(base)
                el = time.time() - t
                tests = len(info) if isinstance(info, list) else info["tests"]
                sm, _ = sim(mini)
                one_min, _ = is_one_minimal(mini, fails)
                rec[name] = {"length": len(mini["words"]), "retired": sm.get("retired"), "cycles": sm.get("cycles"),
                             "tests": tests, "seconds": round(el, 2), "one_minimal": one_min, "still_fails": fails(mini),
                             "levels": None if isinstance(info, list) else info["levels"]}
            rec["length_reduction_pct"] = round(100 * (1 - rec["hdd"]["length"] / rec["original_length"]), 1)
            rec["cycle_reduction_pct"] = round(100 * (1 - (rec["hdd"]["cycles"] or 0) / max(1, rec["original_cycles"])), 1)
            results.append(rec)
            print(f"{bug:40s} {r['method']:9s} s{r['seed']}: {rec['original_length']}->{rec['slice_length']}"
                  f"(slice {'ok' if slice_ok else 'LOST'}) ddmin {rec['ddmin']['length']}/{rec['ddmin']['tests']}t "
                  f"hdd {rec['hdd']['length']}/{rec['hdd']['tests']}t cycles {rec['original_cycles']}->{rec['hdd']['cycles']}", flush=True)
        shutil.rmtree(work, ignore_errors=True)       # a Verilator build is ~250 MB; /tmp may be a small tmpfs
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
