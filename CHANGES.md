# v0.2 audit: what changed and why

Replace these files: `sw_reference/golden_rv32i.py`, `harness/riscv_cosim.py`, `harness/delta_debugger.py`,
`harness/pico_campaign.py`, `harness/run_experiments.py`, `README.md`. Add `tests/`, `tools/apply_fixes.py`.
Then run `python3 tools/apply_fixes.py` (RTL `mem_la_addr` revert + dashboard fixes).
`bug_injector.py`, `bug_manifest.json`, seeds and requirements are unchanged.

## Not verified (no Verilator/Yosys in the audit environment)
C++ harness edits, the Verilator `.dat` coverage format assumption, the Yosys script, and the three
repo-level tests in `tests/test_repo_integrity.py`. Run the suite and `--experiment E0` first.

## Defects fixed (see report Appendix "Code Audit Log" for effects)
1. Build config: divider was not enabled but ISS/validator/prompt allow DIV/REM -> `HW_PARAMS`, `ENABLE_DIV=1`.
2. Scoreboard ignored a DUT that trapped/stopped early; extra retirements ignored -> `missing_retire`/`extra_retire`.
3. `mem_addr` convention: scoreboard now compares the word-aligned address (PicoRV32/riscv-formal ALIGNED_MEM);
   RTL `mem_la_addr` restored to upstream (it had been edited to `reg_op1`).
4. Validator: all R-type funct7, memory window (64 KiB wrap), program overlap, termination, misaligned/out-of-range PCs.
5. Simulator rebuild by content hash (mtime reused stale binaries); lint no longer overwrites simulator sources.
6. C++: `uint64_t`, `<algorithm>`, dynamic retire count, proportional cycle budget.
7. Delta debugger: snapshot after K-3 (was one step early); dynamic trace instead of `words[K-2:K+1]`; memory preload.
8. Real ddmin replaces the cosmetic shrink schedule.
9. Localiser: correct anchors, DIV vs MUL, multi-candidate output (ALU vs regfile ambiguity).
10. Repair: block-patch protocol (was: whole 3000-line file from a 60-line excerpt); stricter acceptance rule;
    golden recovery off by default and never counted as repair; harness errors never trigger repair.
11. Corpus: seeds pinned, invalid fallbacks not appended, accurate `llm_called`, index-based gating.
12. Fuzzer: `reg_alias` mask (cleared funct3), seeded fallback mutator.
13. Bug-targeted stimuli: three of five could not trigger their bug; single generator now.
14. Coverage: cumulative Verilator merge via `.dat` parser; correct denominators; no zero-pattern inflation.
15. Yosys parsing, telemetry build flags, VCD accumulation, dashboard legacy keys, crash status, Gemini history growth.
16. Experiments: broader E0, injection errors reported, timing excludes compile, E6 containment metric.
