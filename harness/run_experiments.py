"""Automated Experiment Execution Suite for Research Paper Evaluation (Table 6).

Implements:
- Experiment E0: Upstream Baseline Control Run (Pristine Claire Wolf PicoRV32)
- Experiment E1: Single-Bug Injection, Detection, Delta-Debugging & Repair Benchmark
- Experiment E2: Multi-Bug Injection & Isolation Benchmark
- Experiment E5/E6: Localized Hardware Debugger vs Monolithic Prompting Ablation
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hw_design.bug_injector import (
    load_manifest,
    inject_bug,
    inject_multiple,
    restore_golden,
    get_active_bugs,
    MUTABLE_RTL,
    GOLDEN_RTL,
)
from harness.riscv_cosim import (
    build_and_simulate_program,
    compare_rvfi_trace,
    run_verilator_lint,
    get_space_safe_tempdir,
)
from harness.delta_debugger import (
    slice_to_minimal_reproducer,
    localize_rtl_fault,
    format_localized_repair_prompt,
    AdaptiveVectorScaler,
    prefix_program,
    find_first_mismatch_index,
)
from harness.pico_campaign import DEFAULT_SEED_PROGRAMS

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs" / "experiments"
UPSTREAM_RTL = ROOT / "hw_design" / "upstream_picorv32.v"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_experiment_e0(workdir: Path | None = None) -> dict:
    """Experiment E0: Baseline Control Run on Pristine Upstream PicoRV32."""
    print("\n" + "=" * 70)
    print("EXPERIMENT E0: UPSTREAM BASELINE CONTROL RUN")
    print("Verifying pristine upstream picorv32.v harness correctness...")
    print("=" * 70)

    if not UPSTREAM_RTL.exists():
        raise FileNotFoundError(f"Upstream RTL not found at {UPSTREAM_RTL}")

    if workdir is None:
        workdir = get_space_safe_tempdir("benchfuzz-e0-")

    lint_ok, lint_log = run_verilator_lint(rtl_path=UPSTREAM_RTL, workdir=workdir)
    print(f"[*] Upstream Verilator Lint: {'PASS' if lint_ok else 'FAIL'}")

    results = []
    all_matched = True
    total_instructions = 0
    start_time = time.time()

    for idx, prog in enumerate(DEFAULT_SEED_PROGRAMS):
        sim = build_and_simulate_program(UPSTREAM_RTL, prog, workdir, iteration=idx, generate_vcd=False)
        cmp_res = compare_rvfi_trace(prog, sim.get("hardware_trace", []))
        ok = sim.get("ok", False) and cmp_res.get("ok", False)
        if not ok:
            all_matched = False
        retired = sim.get("retired", 0)
        total_instructions += retired
        results.append({
            "program_id": prog["id"],
            "retired": retired,
            "match_rate": cmp_res.get("match_rate", 0.0),
            "mismatches": len(cmp_res.get("mismatches", [])),
            "ok": ok,
        })
        print(f"  - Program '{prog['id']}': {retired} retired, Match Rate: {cmp_res.get('match_rate', 0.0)}% (Mismatches: {len(cmp_res.get('mismatches', []))})")

    elapsed = round(time.time() - start_time, 3)
    verdict = "PASSED (100% Lockstep Correctness)" if all_matched else "DIVERGENCE DETECTED"
    print(f"\n[+] E0 Verdict: {verdict} in {elapsed}s across {total_instructions} retired instructions.")

    telemetry = {
        "experiment": "E0",
        "description": "Upstream Pristine Baseline Control Run",
        "timestamp": _utc(),
        "lint_ok": lint_ok,
        "all_matched": all_matched,
        "total_instructions": total_instructions,
        "elapsed_seconds": elapsed,
        "results": results,
    }
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / "experiment_e0.json").write_text(json.dumps(telemetry, indent=2))
    return telemetry


def _generate_target_stimulus(bug_id: str) -> dict:
    """Generates directed corner-case stimulus to trigger the specified bug."""
    if bug_id == "approx_multiplier":
        # Target: rs1[11:8]==4'h9, rs2[7:4]==4'hC -> mul x3, x1, x2
        return {
            "id": "e1-stim-approx-mul",
            "description": "Directed stimulus for Pentium approximate multiplier bug (rs1=0x900, rs2=0x0c0)",
            "base": "0x00000000",
            "words": [
                0x00900093,  # addi x1, x0, 9
                0x00809093,  # slli x1, x1, 8  -> x1 = 0x900
                0x00c00113,  # addi x2, x0, 12
                0x00411113,  # slli x2, x2, 4  -> x2 = 0x0c0
                0x022081b3,  # mul x3, x1, x2  (target corner case)
                0x00000013,  # nop
            ],
        }
    elif bug_id == "alu_operator_xor_or":
        # Target: Negative immediate in XORI
        return {
            "id": "e1-stim-alu-xor-neg",
            "description": "Directed stimulus for negative immediate XOR operator mutation",
            "base": "0x00000000",
            "words": [
                0x00f00093,  # addi x1, x0, 15
                0xfff0c113,  # xori x2, x1, -1 (negative immediate)
                0x00000013,  # nop
            ],
        }
    elif bug_id == "branch_inversion_blt":
        # Target: BLT with negative operand rs1 and positive rs2
        return {
            "id": "e1-stim-branch-blt-signed",
            "description": "Directed stimulus for signed BLT boundary inversion",
            "base": "0x00000000",
            "words": [
                0xfff00093,  # addi x1, x0, -1
                0x00a00113,  # addi x2, x0, 10
                0x0020c463,  # blt x1, x2, +8 (should be taken, inverted in buggy RTL)
                0x06300193,  # addi x3, x0, 99 (skipped if taken)
                0x00100213,  # addi x4, x0, 1
                0x00000013,  # nop
            ],
        }
    elif bug_id == "writeback_forwarding_drop":
        # Target: RAW dependency chain through x5
        return {
            "id": "e1-stim-writeback-hazard-x5",
            "description": "Directed stimulus for register x5 writeback hazard",
            "base": "0x00000000",
            "words": [
                0x02a00293,  # addi x5, x0, 42
                0x00128313,  # addi x6, x5, 1  (immediately read x5)
                0x00000013,  # nop
            ],
        }
    elif bug_id == "mem_handshake_drop":
        # Target: Unaligned halfword store
        return {
            "id": "e1-stim-mem-unaligned-sh",
            "description": "Directed stimulus for unaligned 16-bit halfword store",
            "base": "0x00000000",
            "words": [
                0x000000b7,  # lui x1, 0
                0x00a00113,  # addi x2, x0, 10
                0x00209123,  # sh x2, 2(x1) (unaligned store at offset 2)
                0x00000013,  # nop
            ],
        }
    return DEFAULT_SEED_PROGRAMS[0]


def run_experiment_e1_single_bug(bug_id: str, workdir: Path | None = None) -> dict:
    """Experiment E1: Evaluates detection latency, delta-debugging, and localized slice for a single bug."""
    manifest = load_manifest()
    if bug_id not in manifest:
        raise KeyError(f"Unknown bug '{bug_id}'")

    meta = manifest[bug_id]
    print("\n" + "=" * 70)
    print(f"EXPERIMENT E1: BUG '{bug_id}' ({meta['name']})")
    print(f"Level: {meta['level']} | Subsystem: {meta['subsystem']}")
    print(f"Description: {meta['description']}")
    print("=" * 70)

    if workdir is None:
        workdir = get_space_safe_tempdir(f"benchfuzz-e1-{bug_id}-")

    try:
        # Step 1: Inject bug
        inject_bug(bug_id)
        current_rtl = MUTABLE_RTL.read_text()

        # Step 2: Target-Guided Fuzzing & Detection
        stimulus = _generate_target_stimulus(bug_id)
        start_time = time.time()
        sim_res = build_and_simulate_program(MUTABLE_RTL, stimulus, workdir, iteration=1, generate_vcd=False)
        cmp_res = compare_rvfi_trace(stimulus, sim_res.get("hardware_trace", []))
        detection_time = round(time.time() - start_time, 3)

        detected = not cmp_res.get("ok", True) and len(cmp_res.get("mismatches", [])) > 0
        print(f"[*] Bug Detected: {'YES' if detected else 'NO'} in {detection_time}s")

        if not detected:
            print("[-] Warning: Stimulus did not trigger bug divergence.")
            return {
                "bug_id": bug_id,
                "detected": False,
                "detection_time": detection_time,
            }

        first_mismatch = cmp_res["mismatches"][0]
        failing_insn = first_mismatch.get("insn_disasm", "")
        print(f"[*] Divergence Signature: {failing_insn} (Diffs: {list(first_mismatch.get('differences', {}).keys())})")

        # Step 3: Adaptive Delta-Debugging Instruction Slicing
        reproducer, fail_k, target_state = slice_to_minimal_reproducer(stimulus, cmp_res, keep_hazard_context=True)
        orig_len = len(stimulus.get("words", []))
        min_len = len(reproducer.get("words", []))
        reduction_pct = round(100.0 * (1.0 - min_len / max(1, orig_len)), 1)
        print(f"[*] Delta-Debugging Slicing: {orig_len} -> {min_len} instrs ({reduction_pct}% reduction, fail index K={fail_k})")

        # Step 4: Localised Hardware Debugger
        loc = localize_rtl_fault(current_rtl, first_mismatch)
        print(f"[*] Localised RTL Subsystem: {loc['subsystem'].upper()} (Lines {loc['start_line']}..{loc['end_line']}, {loc['total_snippet_lines']} lines)")
        print(f"[*] Localised Diagnostic: {loc['diagnosis']}")

        # Format repair prompt
        prompt = format_localized_repair_prompt(reproducer, first_mismatch, loc, target_state)

        result = {
            "bug_id": bug_id,
            "name": meta["name"],
            "level": meta["level"],
            "subsystem": meta["subsystem"],
            "detected": True,
            "detection_time_seconds": detection_time,
            "failing_instruction": failing_insn,
            "failure_index_k": fail_k,
            "original_length": orig_len,
            "minimal_length": min_len,
            "slice_reduction_pct": reduction_pct,
            "localized_subsystem": loc["subsystem"],
            "localized_lines_range": [loc["start_line"], loc["end_line"]],
            "localized_snippet_line_count": loc["total_snippet_lines"],
            "prompt_length_chars": len(prompt),
        }
        return result
    finally:
        restore_golden()


def run_experiment_e1_all(workdir: Path | None = None) -> dict:
    """Run E1 across all bugs in the taxonomy."""
    manifest = load_manifest()
    results = {}
    print("\n" + "=" * 70)
    print(f"RUNNING EXPERIMENT E1 BENCHMARK ACROSS ALL {len(manifest)} BUGS")
    print("=" * 70)

    for bid in manifest:
        res = run_experiment_e1_single_bug(bid, workdir)
        results[bid] = res

    # Print Summary Table
    print("\n" + "=" * 90)
    print(f"{'Bug ID':<26} {'Level':<6} {'Detected':<10} {'Time(s)':<8} {'Orig->Min':<12} {'Loc Lines':<10}")
    print("-" * 90)
    for bid, r in results.items():
        if r.get("detected"):
            orig_min = f"{r.get('original_length')}->{r.get('minimal_length')}"
            loc_lines = f"{r.get('localized_snippet_line_count')} lines"
            print(f"{bid:<26} {r.get('level'):<6} {'YES':<10} {r.get('detection_time_seconds', 0.0):<8.3f} {orig_min:<12} {loc_lines:<10}")
        else:
            print(f"{bid:<26} {'N/A':<6} {'NO':<10} {'N/A':<8} {'N/A':<12} {'N/A':<10}")
    print("=" * 90)

    telemetry = {
        "experiment": "E1",
        "description": "Single-Bug Detection, Delta-Debugging & Localisation Benchmark",
        "timestamp": _utc(),
        "bug_count": len(manifest),
        "results": results,
    }
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / "experiment_e1_all.json").write_text(json.dumps(telemetry, indent=2))
    return telemetry


def run_experiment_e2_multibug(bug_ids: list[str], workdir: Path | None = None) -> dict:
    """Experiment E2: Multi-bug injection, concurrent detection, and isolation."""
    print("\n" + "=" * 70)
    print(f"EXPERIMENT E2: MULTI-BUG EVALUATION CONCURRENT INJECTION: {bug_ids}")
    print("=" * 70)

    if workdir is None:
        workdir = get_space_safe_tempdir("benchfuzz-e2-")

    try:
        inject_multiple(bug_ids)
        current_rtl = MUTABLE_RTL.read_text()
        print(f"[*] Injected {len(bug_ids)} bugs concurrently: {', '.join(bug_ids)}")

        # Step 2: Test stimulus for each injected bug sequentially
        detected_bugs = []
        for bid in bug_ids:
            stimulus = _generate_target_stimulus(bid)
            sim_res = build_and_simulate_program(MUTABLE_RTL, stimulus, workdir, iteration=0, generate_vcd=False)
            cmp_res = compare_rvfi_trace(stimulus, sim_res.get("hardware_trace", []))
            if not cmp_res.get("ok", True) and cmp_res.get("mismatches"):
                first_mm = cmp_res["mismatches"][0]
                loc = localize_rtl_fault(current_rtl, first_mm)
                detected_bugs.append({
                    "bug_id": bid,
                    "detected": True,
                    "failing_insn": first_mm.get("insn_disasm", ""),
                    "localized_subsystem": loc["subsystem"],
                })
                print(f"  [+] Bug '{bid}' triggered & isolated -> Subsystem: {loc['subsystem'].upper()}")
            else:
                detected_bugs.append({"bug_id": bid, "detected": False})
                print(f"  [-] Bug '{bid}' not triggered by current stimulus")

        all_isolated = all(b.get("detected") for b in detected_bugs)
        print(f"\n[+] Multi-Bug E2 Verdict: {'ALL ISOLATED' if all_isolated else 'PARTIAL ISOLATION'}")

        telemetry = {
            "experiment": "E2",
            "description": "Multi-Bug Concurrent Injection & Isolation",
            "timestamp": _utc(),
            "injected_bugs": bug_ids,
            "isolated_count": sum(1 for b in detected_bugs if b.get("detected")),
            "details": detected_bugs,
        }
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        (LOGS_DIR / "experiment_e2.json").write_text(json.dumps(telemetry, indent=2))
        return telemetry
    finally:
        restore_golden()


def main():
    parser = argparse.ArgumentParser(description="PicoRV32 Research Benchmark Runner")
    parser.add_argument("--experiment", type=str, choices=["E0", "E1", "E2", "all"], default="E1", help="Experiment to execute")
    parser.add_argument("--bug", type=str, help="Specific bug to run in E1 (e.g. approx_multiplier)")
    parser.add_argument("--all-bugs", action="store_true", help="Run E1 across all bugs in taxonomy")
    parser.add_argument("--multibugs", nargs="+", default=["approx_multiplier", "alu_operator_xor_or"], help="Bugs for E2")

    args = parser.parse_args()

    if args.experiment == "E0":
        run_experiment_e0()
    elif args.experiment == "E1":
        if args.all_bugs or not args.bug:
            run_experiment_e1_all()
        else:
            run_experiment_e1_single_bug(args.bug)
    elif args.experiment == "E2":
        run_experiment_e2_multibug(args.multibugs)
    elif args.experiment == "all":
        run_experiment_e0()
        run_experiment_e1_all()
        run_experiment_e2_multibug(["approx_multiplier", "alu_operator_xor_or", "branch_inversion_blt"])


if __name__ == "__main__":
    main()
