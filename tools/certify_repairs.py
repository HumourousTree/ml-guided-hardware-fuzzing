#!/usr/bin/env python3
"""Formally certify the patches accepted by harness.template_repair (adds a "formal" verdict to each record).

  python3 tools/certify_repairs.py [--results logs/experiments/repair_baselines.json] [--timeout 300]

Accepted candidates are taken from logs/experiments/repair_candidates/ when saved; otherwise they are re-derived
deterministically (oracle patches restore the golden text; a template patch is the template variant of the recorded line
whose text matches the logged prefix), then checked with Yosys equivalence against the golden RTL.  It also runs the
directed witness for any candidate listed in WITNESSES and records whether it exposes the patch as wrong.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.pico_campaign import _i, _r, NOP  # noqa: E402
from harness.riscv_cosim import build_and_simulate_program, compare_rvfi_trace, get_space_safe_tempdir  # noqa: E402
from harness.template_repair import formal_equivalence, template_variants  # noqa: E402
from hw_design.bug_injector import GOLDEN_RTL, injected_text  # noqa: E402

# Directed programs that distinguish a patch from the golden design when formal equivalence is not proven.
# approx_multiplier: the injected trigger is rs1[11:8]==9 && rs2[7:4]==0xC; a patch that inverts the first test
# corrupts bit 5 for rs1[11:8]!=9, e.g. 0x100 * 0xC0.
WITNESSES = {"approx_multiplier": [_i(0x100, 0, 0, 1), _i(0xC0, 0, 0, 2), _r(1, 2, 1, 0, 3), NOP]}


def reconstruct(bug: str, model: str, rec: dict, golden: str) -> str | None:
    saved = Path(rec.get("candidate_file") or f"logs/experiments/repair_candidates/{bug}__{model}.sv")
    if saved.exists():
        return saved.read_text()
    if rec.get("exact_fix"):
        return golden
    patch = rec.get("accepted_patch") or {}
    if model != "template" or not patch:
        return None
    lines = injected_text([bug]).splitlines()
    n = patch["line"]
    match = [v for v in template_variants(lines[n - 1]) if v.strip()[:160] == patch["patch"]]
    if len(match) != 1:
        return None
    lines[n - 1:n] = match[0].splitlines()
    return "\n".join(lines) + "\n"


def witness(bug: str, cand: str) -> dict | None:
    if bug not in WITNESSES:
        return None
    work = get_space_safe_tempdir("witness-")
    rtl = work / "cand.sv"; rtl.write_text(cand)
    prog = {"id": f"witness-{bug}", "base": "0x00000000", "words": [f"0x{w:08x}" for w in WITNESSES[bug]]}
    sim = build_and_simulate_program(rtl, prog, work, generate_vcd=False)
    cmp = compare_rvfi_trace(prog, sim.get("hardware_trace", []), hardware_trap=sim.get("hardware_trap", False))
    mm = (cmp["mismatches"] or [None])[0]
    return {"exposes_patch": bool(mm), "mismatch": mm and {k: mm.get(k) for k in ("insn_disasm", "differences")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="logs/experiments/repair_baselines.json")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    golden = GOLDEN_RTL.read_text()
    res = json.loads(Path(a.results).read_text())
    for key, rec in res.items():
        if not rec.get("accepted"):
            continue
        bug, model = key.split("|")
        cand = reconstruct(bug, model, rec, golden)
        if cand is None:
            rec["formal"] = {"verdict": "unavailable"}
        elif "formal" not in rec or rec["formal"].get("verdict") == "unavailable":
            rec["formal"] = formal_equivalence(golden, cand, a.timeout)
        if cand is not None and rec["formal"]["verdict"] == "not_proven":
            rec["witness"] = witness(bug, cand)
        print(f"{key:50s} {rec['formal']} {rec.get('witness') or ''}", flush=True)
        Path(a.results).write_text(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
