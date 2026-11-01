"""Experiment runner for the evaluation plan (E0 control, E1 single-bug, E2 multi-bug).

Fixes versus the prototype: E0 now also runs fuzzed, RV32M and bug-stimulus programs; injection failures are
reported instead of silently running a clean RTL; detection time excludes Verilator compilation; the
reproducer is verified and minimised against the buggy DUT; and E6 (does the localised block contain the
injected fault?) is measured.
"""

import argparse
import json
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from hw_design.bug_injector import (load_manifest, inject_bug, inject_multiple, restore_golden,
                                    MUTABLE_RTL, GOLDEN_RTL, edit_lines)
from harness.riscv_cosim import (build_and_simulate_program, compare_rvfi_trace, run_verilator_lint,
                                 get_space_safe_tempdir, prepare_simulator, validate_program)
from harness.delta_debugger import slice_to_minimal_reproducer, ddmin_reproducer, localize_rtl_fault
from harness.pico_campaign import (DEFAULT_SEED_PROGRAMS, _generate_bug_targeted_stimulus,
                                   _directed_mutate_program, _prog, _r, _i, _s, _lui, NOP)

SUITE = "legacy"          # set by --suite

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs" / "experiments"
UPSTREAM_RTL = ROOT / "hw_design" / "upstream_picorv32.v"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(name: str, data: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / name).write_text(json.dumps(data, indent=2, default=str))


def _run(rtl: Path, prog: dict, workdir: Path, delay: int = 0):
    sim = build_and_simulate_program(rtl, prog, workdir, generate_vcd=False, mem_delay=delay)
    cmp = compare_rvfi_trace(prog, sim.get("hardware_trace", []), hardware_trap=sim.get("hardware_trap", False))
    return sim, cmp


def _m_ext_program() -> dict:
    """RV32M coverage for the control run (DIV/REM edge cases included)."""
    words = [_lui(1, 0x80000), _i(-1, 0, 0, 2), _r(1, 2, 1, 4, 3), _r(1, 2, 1, 6, 4), _r(1, 0, 1, 5, 5),
             _r(1, 2, 1, 0, 6), _r(1, 2, 1, 1, 7), _r(1, 2, 1, 2, 8), _r(1, 2, 1, 3, 9), NOP]
    return _prog("e0-m-extension", "MUL/MULH/MULHSU/MULHU/DIV/DIVU/REM edge cases", words)


def alignment_probe_programs() -> list[dict]:
    """Loads/stores at every byte offset: the case that exposed the 2019-vs-2016 `mem_addr` discrepancy."""
    prog = [_i(0x100 // 1, 0, 0, 1), _i(0x7B, 0, 0, 2), _s(0, 2, 1, 2)]                   # x1=0x100, x2=0x7b, sw x2,0(x1)
    prog += [_i(off, 1, 0, 3 + k % 3, opcode=0x03) for k, off in enumerate((1, 2, 3))]       # lb at odd offsets
    prog += [_i(2, 1, 1, 7, opcode=0x03), _i(2, 1, 5, 8, opcode=0x03), _s(3, 2, 1, 0), _s(2, 2, 1, 1)]   # lh, lhu, sb @3, sh @2
    prog.append(NOP)
    return [_prog("e0-alignment-probe", "byte/halfword accesses at non-zero byte offsets", prog)]


def fence_probe_program() -> dict:
    """Cascade P6 (CVE-2023-34899): PicoRV32 f00a88c raises a spurious exception on FENCE; fixed upstream in 29102c0."""
    fence = 0x0FF0000F                                   # fence iorw, iorw
    return _prog("e0-fence-probe", "FENCE between ALU ops (Cascade bug P6)",
                 [_i(5, 0, 0, 1), fence, _i(7, 1, 0, 2), fence, _r(0, 2, 1, 0, 3), NOP])


def fence_rd_probe_program() -> dict:
    """FENCE with the reserved rd field set (x1).  The spec says base implementations shall ignore rd; PicoRV32 master
    writes 0 to it (found by the mutation fuzzer; cf. YosysHQ/picorv32#293, closed as having no side effect)."""
    return _prog("e0-fence-rd-probe", "x1=123; fence (rd=x1); addi x2, x1, 0",
                 [_i(123, 0, 0, 1), 0x0FF0008F, _i(0, 1, 0, 2), NOP])


def run_experiment_e0(workdir: Path | None = None, fuzz_per_seed: int = 10, upstream: Path | None = None,
                      tag: str = "") -> dict:
    """E0: the unmodified upstream core must match the ISS 100% on seeds, fuzzed, RV32M and bug-stimulus programs."""
    UPSTREAM_RTL = Path(upstream) if upstream else globals()["UPSTREAM_RTL"]
    print("\n" + "=" * 70 + f"\nEXPERIMENT E0: UPSTREAM BASELINE CONTROL RUN ({UPSTREAM_RTL.name})\n" + "=" * 70)
    if not UPSTREAM_RTL.exists():
        raise FileNotFoundError(f"Upstream RTL not found at {UPSTREAM_RTL}")
    commit_file = Path(str(UPSTREAM_RTL) + ".commit")
    upstream_commit = commit_file.read_text().split()[0] if commit_file.exists() else None
    workdir = workdir or get_space_safe_tempdir("benchfuzz-e0-")
    lint_ok, _ = run_verilator_lint(rtl_path=UPSTREAM_RTL, workdir=workdir)
    rng = random.Random(1234)
    programs = list(DEFAULT_SEED_PROGRAMS) + [_m_ext_program()]
    programs += [p for p in (_generate_bug_targeted_stimulus(b) for b in load_manifest("all")) if p]
    programs += alignment_probe_programs()
    for seed_prog in DEFAULT_SEED_PROGRAMS:
        programs += [_directed_mutate_program(seed_prog, rng, k, []) for k in range(fuzz_per_seed)]

    results, total, ok_all, t0 = [], 0, True, time.time()
    for delay in (0, 1, 3):
        for prog in programs:
            sim, cmp = _run(UPSTREAM_RTL, prog, workdir, delay)
            ok = bool(sim["ok"] and cmp["ok"])
            ok_all &= ok
            total += sim.get("retired", 0)
            results.append({"program_id": prog["id"], "mem_delay": delay, "retired": sim.get("retired", 0),
                            "ok": ok, "error": sim.get("error"),
                            "first_mismatch": (cmp["mismatches"] or [None])[0] and
                                              {k: cmp["mismatches"][0].get(k) for k in ("kind", "insn_disasm", "differences", "error")}})
            if not ok:
                print(f"  [FAIL] {prog['id']} delay={delay}: {results[-1]['error'] or results[-1]['first_mismatch']}")
    # --- convention probe: does the *unmodified upstream* core report word-aligned mem_addr? ---------------------------
    probe_prog = alignment_probe_programs()[0]
    psim = build_and_simulate_program(UPSTREAM_RTL, probe_prog, workdir, generate_vcd=False)
    raw = compare_rvfi_trace(probe_prog, psim.get("hardware_trace", []), aligned_mem=False)      # architectural address expected
    norm = compare_rvfi_trace(probe_prog, psim.get("hardware_trace", []), aligned_mem=True)      # word-aligned address expected
    raw_fields = sorted((raw["mismatches"][0].get("differences") or {}).keys()) if raw["mismatches"] else []
    if norm["ok"] and raw_fields == ["mem_addr"]:
        conclusion = ("CONVENTION GAP: unmodified upstream reports word-aligned mem_addr (e.g. 2016 for byte address 2019); "
                      "it passes lockstep once the scoreboard applies the aligned convention. Not a DUT defect.")
    elif norm["ok"]:
        conclusion = "Upstream matches under both conventions on this probe (probe does not exercise unaligned offsets?)."
    else:
        conclusion = "UNEXPECTED: upstream fails even with the aligned convention - investigate as a harness/ISS or DUT defect."
    # --- Cascade P6 probe (reported separately: f00a88c is *expected* to fail it) -------------------------------------
    fprog = fence_probe_program()
    fsim, fcmp = _run(UPSTREAM_RTL, fprog, workdir)
    fmm = (fcmp["mismatches"] or [None])[0]
    fence_probe = {"ok": bool(fsim["ok"] and fcmp["ok"]), "hardware_trap": fsim.get("hardware_trap"), "error": fsim.get("error"),
                   "first_mismatch": fmm and {k: fmm.get(k) for k in ("kind", "insn_disasm", "differences", "error")}}
    print(f"[+] E0 Cascade-P6 FENCE probe: {'matches' if fence_probe['ok'] else 'DIVERGES: ' + str(fence_probe['first_mismatch'] or fence_probe['error'])}")
    rprog = fence_rd_probe_program()
    rsim, rcmp = _run(UPSTREAM_RTL, rprog, workdir)
    rmm = (rcmp["mismatches"] or [None])[0]
    x2 = next((r.get("rd_wdata") for r in rsim.get("hardware_trace", []) if r.get("rd_addr") == 2), None)
    fence_rd_probe = {"ok": bool(rsim["ok"] and rcmp["ok"]), "hardware_trap": rsim.get("hardware_trap"),
                      "x2_after_fence": x2, "x2_expected": 123,
                      "first_mismatch": rmm and {k: rmm.get(k) for k in ("kind", "insn_disasm", "differences", "error")}}
    print(f"[+] E0 FENCE-rd probe: {'matches' if fence_rd_probe['ok'] else 'DIVERGES: ' + str(fence_rd_probe['first_mismatch'])}; x2={x2} (expected 123)")
    golden_equal = GOLDEN_RTL.exists() and GOLDEN_RTL.read_text() == UPSTREAM_RTL.read_text()
    print(f"[+] E0 probe: raw-convention mismatch fields={raw_fields}, aligned-convention ok={norm['ok']}\n    {conclusion}\n    golden == upstream: {golden_equal}")
    verdict = "PASSED (100% lockstep)" if ok_all else "DIVERGENCE: harness/ISS convention defect"
    n_ok = sum(r["ok"] for r in results)
    print(f"[+] E0: {verdict}; {n_ok}/{len(results)} runs matched, {total} retired instructions, {time.time() - t0:.1f}s")
    tel = {"experiment": "E0", "timestamp": _utc(), "lint_ok": lint_ok, "all_matched": ok_all,
           "upstream_file": UPSTREAM_RTL.name, "upstream_commit": upstream_commit,
           "programs": len(programs), "mem_delays": [0, 1, 3], "runs_matched": n_ok,
           "match_rate_pct": round(100.0 * n_ok / max(1, len(results)), 2), "wall_seconds": round(time.time() - t0, 1),
           "convention_probe": {"raw_mismatch_fields": raw_fields, "aligned_convention_ok": norm["ok"], "conclusion": conclusion,
                                "first_raw_mismatch": (raw["mismatches"] or [None])[0] and {k: raw["mismatches"][0].get(k) for k in ("insn_disasm", "differences")}},
           "cascade_p6_fence_probe": fence_probe, "fence_rd_probe": fence_rd_probe,
           "golden_equals_upstream": golden_equal, "upstream_sha256": __import__("hashlib").sha256(UPSTREAM_RTL.read_bytes()).hexdigest(),
           "runs": len(results), "total_instructions": total, "results": results}
    _save(f"experiment_e0{tag}.json", tel)
    shutil.rmtree(workdir, ignore_errors=True)
    return tel


def _fault_line(rtl: str, entry: dict) -> int | None:
    pos = rtl.find(entry["replacement"])
    return None if pos < 0 else rtl[:pos].count("\n") + 1


def run_experiment_e1_single_bug(bug_id: str, workdir: Path | None = None) -> dict:
    manifest = load_manifest(SUITE)
    if bug_id not in manifest:
        raise KeyError(f"Unknown bug '{bug_id}'")
    meta = manifest[bug_id]
    print(f"\nE1: {bug_id} ({meta['name']}) level={meta['level']} subsystem={meta['subsystem']}")
    cleanup = workdir is None
    workdir = workdir or get_space_safe_tempdir(f"benchfuzz-e1-{bug_id}-")
    try:
        if not inject_bug(bug_id, SUITE):
            return {"bug_id": bug_id, "detected": False, "error": "injection failed (marker not found)"}
        rtl = MUTABLE_RTL.read_text()
        stim = _generate_bug_targeted_stimulus(bug_id)
        if not validate_program(stim)["valid"]:
            return {"bug_id": bug_id, "detected": False, "error": "stimulus invalid"}

        ok, log = prepare_simulator(MUTABLE_RTL, workdir)          # compile outside the timed region
        if not ok:
            return {"bug_id": bug_id, "detected": False, "error": "RTL does not build: " + log[-300:]}
        t0 = time.time()
        sim, cmp = _run(MUTABLE_RTL, stim, workdir)
        detect_s = round(time.time() - t0, 3)
        detected = bool(cmp["mismatches"])
        print(f"  detected={detected} in {detect_s}s")
        if not detected:
            return {"bug_id": bug_id, "detected": False, "detection_time_seconds": detect_s}

        mm = cmp["mismatches"][0]
        validator = lambda p: validate_program(p)["valid"]
        repro, K, _ = slice_to_minimal_reproducer(stim, cmp, True, validator)
        def fails(p: dict) -> bool:
            # an invalid candidate yields an empty RTL trace, which the scoreboard reports as missing_retire;
            # that must not count as "still fails" (pico_campaign uses the same rule)
            s, c = _run(MUTABLE_RTL, p, workdir)
            return bool(s["ok"] and c["mismatches"])
        reproduces = fails(repro)
        # A slice preloads architectural state only; a bug living in micro-architectural state (e.g. a stale flag)
        # can vanish from it.  Then minimise the original failing program instead of giving up.
        dd_input = "slice" if reproduces else "original"
        minimal, dd_log = ddmin_reproducer(repro if reproduces else stim, fails)
        msim, mcmp = _run(MUTABLE_RTL, minimal, workdir)
        rec = (mcmp["mismatches"] or [mm])[0]
        loc = localize_rtl_fault(rtl, rec)
        flines = edit_lines(meta, GOLDEN_RTL.read_text())
        fline = flines[-1]
        hit = any(c["start_line"] <= l <= c["end_line"] for l in flines for c in loc["candidates"])
        primary_hit = any(loc["start_line"] <= l <= loc["end_line"] for l in flines)
        orig_len, min_len = len(stim["words"]), len(minimal["words"])
        print(f"  K={K} {orig_len}->{min_len} instrs; localised {loc['subsystem']} lines "
              f"{[(c['start_line'], c['end_line']) for c in loc['candidates']]}; fault line {fline}; hit={hit}")
        return {"bug_id": bug_id, "name": meta["name"], "level": meta["level"], "subsystem": meta["subsystem"],
                "detected": True, "detection_time_seconds": detect_s, "failure_kind": mm.get("kind"),
                "failing_instruction": mm.get("insn_disasm"), "failure_index_k": K, "slice_mode": repro.get("metadata", {}).get("mode"),
                "reproducer_fails": reproduces, "ddmin_input": dd_input, "minimal_fails": bool(mcmp["mismatches"]),
                "original_length": orig_len, "minimal_length": min_len,
                "original_cycles": sim.get("cycles"), "minimal_cycles": msim.get("cycles"), "suite": meta.get("suite"),
                "slice_reduction_pct": round(100.0 * (1 - min_len / max(1, orig_len)), 1), "ddmin_tests": len(dd_log),
                "localized_subsystem": loc["subsystem"], "localization_candidates": [(c["start_line"], c["end_line"]) for c in loc["candidates"]],
                "fault_line": fline, "localization_contains_fault": hit, "primary_contains_fault": primary_hit,
                "localized_snippet_line_count": loc["total_snippet_lines"]}
    finally:
        restore_golden()
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)


def run_experiment_e1_all(workdir: Path | None = None) -> dict:
    manifest = load_manifest(SUITE)
    results = {bid: run_experiment_e1_single_bug(bid, workdir) for bid in manifest}
    print("\n" + "=" * 100)
    print(f"{'Bug ID':<26}{'Lvl':<5}{'Det':<5}{'t(s)':<8}{'Orig->Min':<11}{'Repro':<7}{'Loc':<10}{'Fault in block':<15}")
    for bid, r in results.items():
        if r.get("detected"):
            print(f"{bid:<26}{r['level']:<5}{'Y':<5}{r['detection_time_seconds']:<8.3f}{str(r['original_length']) + '->' + str(r['minimal_length']):<11}"
                  f"{str(r['reproducer_fails']):<7}{r['localized_subsystem']:<10}{str(r['localization_contains_fault']):<15}")
        else:
            print(f"{bid:<26}{'-':<5}{'N':<5}{r.get('error', '')}")
    tel = {"experiment": "E1", "timestamp": _utc(), "bug_count": len(manifest), "results": results}
    _save("experiment_e1_all.json", tel)
    return tel


def run_experiment_e2_multibug(bug_ids: list[str], workdir: Path | None = None) -> dict:
    print(f"\nE2: concurrent injection {bug_ids}")
    cleanup = workdir is None
    workdir = workdir or get_space_safe_tempdir("benchfuzz-e2-")
    try:
        if not inject_multiple(bug_ids, SUITE):
            return {"experiment": "E2", "error": "injection failed", "injected_bugs": bug_ids}
        rtl = MUTABLE_RTL.read_text()
        details = []
        for bid in bug_ids:
            stim = _generate_bug_targeted_stimulus(bid)
            sim, cmp = _run(MUTABLE_RTL, stim, workdir)
            if cmp["mismatches"]:
                loc = localize_rtl_fault(rtl, cmp["mismatches"][0])
                details.append({"bug_id": bid, "detected": True, "failing_insn": cmp["mismatches"][0].get("insn_disasm"),
                                "localized_subsystem": loc["subsystem"]})
            else:
                details.append({"bug_id": bid, "detected": False, "error": sim.get("error")})
            print(f"  {bid}: {'isolated -> ' + details[-1]['localized_subsystem'] if details[-1]['detected'] else 'not triggered'}")
        tel = {"experiment": "E2", "timestamp": _utc(), "injected_bugs": bug_ids,
               "isolated_count": sum(d["detected"] for d in details), "details": details}
        _save("experiment_e2.json", tel)
        return tel
    finally:
        restore_golden()
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="PicoRV32 research benchmark runner")
    ap.add_argument("--experiment", choices=["E0", "E1", "E2", "all"], default="E1")
    ap.add_argument("--suite", choices=["legacy", "v2", "all"], default="legacy", help="bug suite for E1/E2")
    ap.add_argument("--bug")
    ap.add_argument("--all-bugs", action="store_true")
    ap.add_argument("--multibugs", nargs="+", default=["approx_multiplier", "alu_operator_xor_or"])
    ap.add_argument("--upstream", help="E0: RTL to use as the control (default hw_design/upstream_picorv32.v)")
    ap.add_argument("--tag", default="", help="E0: suffix for logs/experiments/experiment_e0<tag>.json")
    a = ap.parse_args()
    global SUITE
    SUITE = a.suite
    if a.experiment == "E0":
        run_experiment_e0(upstream=a.upstream, tag=a.tag)
    elif a.experiment == "E1":
        run_experiment_e1_all() if (a.all_bugs or not a.bug) else run_experiment_e1_single_bug(a.bug)
    elif a.experiment == "E2":
        run_experiment_e2_multibug(a.multibugs)
    else:
        run_experiment_e0(); run_experiment_e1_all()
        run_experiment_e2_multibug(["approx_multiplier", "alu_operator_xor_or", "branch_inversion_blt"])


if __name__ == "__main__":
    main()
