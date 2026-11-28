#!/usr/bin/env python3
"""Minimise the detecting programs of Rocket/BOOM campaigns (capability #5): hierarchical delta debugging of the
program body, with the replay rule as the test.

  python3 rocket/minimize.py --core rocket|boom result.json [...] [--out min.json] [--max-tests 300] [--jobs 4]

For each campaign result with a first detection:
1. The detecting program is recovered by replaying the campaign's random stream (rv64fuzz.generator_args, the seed of
   run_encarsia.fuzz) until a generated program equals the stored first_detection["asm"] byte for byte; this gives
   its generator blocks. Programs of coverage-feedback runs that were mutated are skipped (no stream to replay).
2. Only the body is minimised; prologue (initial registers, FP state, data) and epilogue (dumps) stay as they are.
   Level 1 removes whole generator blocks (branch + its skipped instructions + label, a template instance, the
   generator's multi-line CSR sequences), level 2 single lines of what survives (harness/delta_debugger._ddmin_units).
3. A candidate still fails iff it assembles, Spike finishes, the buggy core shows the same kind of failure
   (mismatch / non-termination), and the bug's bug-free core agrees with Spike: every minimised program is a
   replay-confirmed detection by construction. The result is checked again at the end and tested for 1-minimality
   (removing any single remaining body line makes the failure disappear) when the budget allows.
Output: {result file: {original/minimised body lines, tests, seconds, one_minimal, differing, minimised asm}}.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
sys.path.insert(0, str(ROOT))
import rv64fuzz  # noqa: E402
import run_encarsia  # noqa: E402
from confirm_detections import name as dump_name  # noqa: E402
from harness.delta_debugger import _ddmin_units  # noqa: E402


def fmt(line: str) -> str:
    """How gen_program writes a body line into the program text."""
    return "  " + line if not line.endswith(":") else line


def recover(r: dict, max_programs: int = 5000) -> list[list[str]] | None:
    """The generator blocks of the stored first detecting program (None if it cannot be regenerated)."""
    fd = r["first_detection"]
    if fd.get("mutated"):
        return None
    seed = 1000 * r["seed"] + r["dir"] + (0 if r["profile"] == "base" else 500000)
    os.environ["BENCHFUZZ_CHECKPOINT"] = str(r.get("checkpoint", 0))    # regenerate with the campaign's checkpoints
    _, gargs = rv64fuzz.generator_args(r.get("fp", True), r["profile"], r["core"])
    rng = random.Random(seed)
    for _ in range(max_programs):
        asm, blocks = rv64fuzz.gen_program(rng, r.get("length", 40), *gargs, return_body="blocks")
        if asm == fd["asm"]:
            return blocks
    return None


def splice(asm: str, body: list[str]) -> tuple[list[str], int, int]:
    """Locate the body (formatted lines) inside the program; returns (program lines, start, end)."""
    lines = asm.split("\n")
    want = [fmt(l) for l in body]
    for i in range(len(lines) - len(want) + 1):
        if lines[i:i + len(want)] == want:
            return lines, i, i + len(want)
    raise ValueError("body not found in program text")


def minimize(core: str, path: str, max_tests: int) -> tuple[str, dict]:
    t0 = time.time()
    f = Path(path)
    r = json.loads(f.read_text())
    runs = run_encarsia.paths(core)[1]
    bug_sim = runs / f"{r['kind']}_{r['dir']}" / "Vtop_tiny_soc"
    ref_sim = runs / run_encarsia.golden_name(core, r["kind"], r["dir"]) / "Vtop_tiny_soc"
    blocks = recover(r)
    if blocks is None:
        return f.name, {"skipped": "program not regenerable (mutated or stream mismatch)"}
    fd = r["first_detection"]
    lines, a, b = splice(fd["asm"], [l for blk in blocks for l in blk])
    want_kind = fd["kind"]
    ext = r["profile"] == "ext"
    work = Path(tempfile.mkdtemp(prefix="minimize-"))
    tests = [0]
    last = {}

    def check(body: list[str]) -> bool:
        tests[0] += 1
        asm = "\n".join(lines[:a] + [fmt(l) for l in body] + lines[b:])
        try:
            elf = rv64fuzz.build_elf(asm, work)
        except Exception:
            return False
        exp = rv64fuzz.run_spike(elf)
        if exp is None:
            return False
        got, stop = rv64fuzz.run_rocket(bug_sim, elf, 400000)
        kind = "nontermination" if not stop else ("mismatch" if got != exp else None)
        if kind != want_kind:
            return False
        rgot, rstop = rv64fuzz.run_rocket(ref_sim, elf, 400000)
        if not rstop or rgot != exp:
            return False
        last.update(asm=asm, differing=[dump_name(i, ext) for i, (x, y) in enumerate(zip(got, exp)) if x != y][:6])
        return True

    orig = [l for blk in blocks for l in blk]
    if not check(orig):
        return f.name, {"skipped": "original program does not fail under the replay rule", "tests": tests[0]}
    kept_blocks, t1 = _ddmin_units(blocks, check, max_tests)
    flat = [[l] for blk in kept_blocks for l in blk]
    kept, t2 = _ddmin_units(flat, check, max(0, max_tests - t1))
    body = [l for u in kept for l in u]
    ok = check(body)                                    # final confirmation (also refreshes asm/differing)
    one_min = None
    if ok:                                              # 1-minimality: costs one test per remaining line
        one_min = not any(check(body[:i] + body[i + 1:]) for i in range(len(body)))
        check(body)
    return f.name, {"kind": want_kind, "profile": r["profile"], "dir": r["dir"], "bug_kind": r["kind"],
                    "seed": r["seed"], "n_programs": fd["n_programs"],
                    "body_lines": len(orig), "blocks": len(blocks), "min_body_lines": len(body),
                    "min_blocks": len(kept_blocks), "confirmed": ok, "one_minimal": one_min,
                    "tests": tests[0], "seconds": round(time.time() - t0, 1), "min_body": body,
                    "differing": last.get("differing"), "min_asm": last.get("asm")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", choices=run_encarsia.CORES, default="rocket")
    ap.add_argument("results", nargs="+")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-tests", type=int, default=300)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    out = Path(a.out or run_encarsia.paths(a.core)[2] / "minimized.json")
    done = json.loads(out.read_text()) if out.exists() else {}
    todo = [p for p in a.results if Path(p).name not in done and json.loads(Path(p).read_text()).get("first_detection")]
    with ProcessPoolExecutor(a.jobs) as ex:
        for fu in as_completed([ex.submit(minimize, a.core, p, a.max_tests) for p in todo]):
            n, v = fu.result()
            done[n] = v
            print(n, {k: v.get(k) for k in ("skipped", "body_lines", "min_body_lines", "one_minimal", "tests",
                                             "seconds", "differing")}, flush=True)
            out.write_text(json.dumps(done, indent=1))


if __name__ == "__main__":
    main()
