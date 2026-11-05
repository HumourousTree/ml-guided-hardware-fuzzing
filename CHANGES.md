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

# v0.3 (post-audit runs, real-RTL checks, benchmark framework)

New: `harness/asm.py` (RV32IM assembler with line-numbered errors), `harness/semantic_gen.py` (ChipFuzzer-style target-line
generation with syntax/semantic correction), `harness/benchmark.py` (random / mutation / semantic / hybrid generators, plateau rule,
campaign runner, statistics), `harness/repair_eval.py` (pass@k, independent samples), `hw_design/bug_manifest_v2.json` +
multi-edit `bug_injector.py` (7 new bugs, tiers L1-L3), `tools/fetch_upstream.py`, `tools/analyze_bench.py`, `tools/uncovered_lines.py`,
`docs/BENCHMARK_PLAN.md`, E0 convention probe, `--suite` for E1/E2.

Fixed after running on the real RTL: the localiser's memory block missed the bus state machine (where `mem_addr` is registered) and the
instruction-launch block (`latched_rd`); both are now offered. All 12 bugs' edits lie inside an offered block (`tests/test_repo_integrity.py`).
Fixed earlier in v0.2.x: acceptance check now re-runs programs at the same memory delay as before the repair; Gemini expansion retry and
statistics; rollback reasons recorded and fed back; fallback mutator retries until valid.

Correction to an earlier statement: the 552-iteration run repaired all five injected bugs (the iteration-14 patch removed two injected
lines), not four. `repaired_bugs` telemetry from v0.2.1 was wrong and was removed in v0.2.2.

Still unverified (no Verilator in the authoring environment): C++ harness, coverage `.dat` format assumption, validity of the 7 new RTL
edits as Verilog (markers/structure checked, never compiled), observability of the new bugs, everything in docs/BENCHMARK_PLAN.md.

# v0.4 (everything run on Verilator 5.032 / Yosys 0.52 in WSL; paper in paper/, compact results in results/v0.4/)

## Headline results
- E0: 150/150 runs match on YosysHQ master ef203c2, on Cascade's f00a88c and on the golden copy; 0 mismatches in the 210,000
  programs of the coverage benchmark. The 2019/2016 `mem_addr` discrepancy is a convention gap present on pristine upstream.
- Cascade P6 (fence trap, CVE-2023-34899) reproduced on f00a88c (probe + blind fuzzing, 10/10 seeds). On current master the
  mutation fuzzer found that a FENCE with a non-zero reserved rd field overwrites rd with an undefined value, 0 in Verilator (9/10 seeds; random 0/10). Related upstream
  issue YosysHQ/picorv32#293 was closed as "no side effect".
- Coverage (7 seeds, 10k programs): mutation 64.57%, hybrid 64.21%, random 63.79% (p<0.002); every reachable line is covered
  within a few hundred programs; all 64 unreached line points are configuration-dead or outside the ISA scope. The ChipFuzzer
  plateau rule fires prematurely (~200 programs).
- Time-to-bug (15 bugs, 10 seeds, 1000 programs): random finds 14/15, mutation 13/15; median latency 1-3 programs in every tier;
  approx_multiplier is hard (2/10), the decode AND->OR mutant is equivalent on valid programs.
- Delta debugging on 80 fuzzer failures: 36 -> 3 instructions median (93%), cycles -86%; all results 1-minimal; HDD rarely beats
  flat ddmin because slicing already shortens programs to ~19 instructions.
- Repair: guard controls pass on 14/14 observable bugs; Ochiai template repair gets 9 gate-passing patches, only 7 formally certified:
  one wrong for rare MUL operands (directed witness), one changes only the `mem_la_write` output (2400/2401 points proven).
- C_U ranks random above mutation although mutation covers more RTL toggle bits: report components.
- PicoRV32 for Zynq UltraScale+ (Yosys synth_xilinx xcup): 1,502 LUTs, 945 FFs, 4 DSP48E2.

## Code changes
Verified: the whole v0.3 "still unverified" list. All 15 bug edits compile; the C++ harness, `.dat` parser and Yosys script work;
75 unit tests pass (`python3 -m unittest discover -s tests -t .`).

Oracle and DUT
- `picorv32_golden.sv` was NOT pristine upstream (REGS_INIT_ZERO default, simplified `mem_la_read`); it is now byte-identical to YosysHQ
  master `ef203c2` (`tools/fetch_upstream.py --commit ef203c2`). Both edits were inert in this configuration (E0 identical).
- E0 on master and on Cascade's commit `f00a88c`: 150/150 runs match. Convention probe: unmodified upstream reports word-aligned
  `mem_addr` (lb 1(x1): 257 expected, 256 actual) -> the 2019/2016 discrepancy is a convention gap.
- FENCE added to reference/validator/assembler as an architectural no-op. Cascade P6 (fence trap) reproduced on f00a88c; on master the
  mutation fuzzer found that a FENCE with a non-zero reserved rd field overwrites rd with an undefined value (cf. YosysHQ/picorv32#293, closed as "no side
  effect"). Both are E0 probes.

Bug suite (15): + `fast_mul_decode_and_to_or` (literal brief; equivalent mutant, never detected), `fast_mul_rs2_signed_widened`,
`store_after_store_dropped_intermittent`. E1: ddmin falls back to the original program when the state-preloading slice loses a
micro-architectural failure; `fails()` requires a valid simulation (an invalid candidate used to count as "still fails").

Benchmark: budgets in programs, parallel-safe `--bug` (private RTL copy), pre-built simulator, per-page coverage series, `--save-programs`,
`--fence-rate`, `--stop-on-mismatch`, mismatch details and failing words saved; per-program artefacts deleted.
`scripts/run_full_evaluation.sh` runs the whole evaluation with N workers.

New: hierarchical delta debugging + 1-minimality (`delta_debugger.hdd_reproducer`, `tools/hdd_eval.py`); guard controls and an
Ochiai-ranked template-repair baseline driving the unchanged repair pipeline (`harness/template_repair.py`); unified co-coverage
(`tools/unified_coverage.py`); unreached-RTL classification (`tools/uncovered_lines.py --universe-dat`); sourced literature values
(`docs/literature_baselines.json`) and the comparative analysis that generates every table/figure/number of the paper
(`tools/compare_literature.py`); IEEE-format paper (`paper/main.tex`).

Not done: anything needing an LLM (semantic/hybrid generation, pass@k) - no API key in the evaluation environment.

# v0.4.1 (repository clean-up)
- Entry points moved to `scripts/` (`run_full_evaluation.sh`, `run_campaign.*`, `start_dashboard.*`); the Windows launchers now
  derive the WSL path from their own location (the old hard-coded path no longer existed).
- Removed: `sw_reference/riscv_iss.py` (unused), `tools/apply_fixes.py` (one-off, fully applied; the golden RTL is now pristine
  upstream), the superseded master "control" run, split repair result files; accepted repair patches are stored as diffs.
- `requirements.txt` lists every third-party package (matplotlib added); `hw_design/picorv32.v` (campaign mirror) is ignored;
  result logs under `results/` are committed. README rewritten with a layout map; `results/v0.4/README.md` describes the data.

# v0.4.2 (LLM budget, paper moved to Overleaf repo)
- `harness/llm_budget.py`: every real Gemini request is metered: per-model daily cap (`GEMINI_DAILY_LIMIT`, default 15),
  minimum interval (`GEMINI_MIN_INTERVAL_S`, default 10 s), a 429/quota error blocks the model until the Pacific-time reset,
  state in `logs/llm_budget.json`, call log in `logs/llm_calls.jsonl`; scripted clients (tests, repair baselines) are not
  metered. Call sites are labelled (`stimulus_expansion`, `rtl_repair`, `semantic_generation`). 5 new tests (80 total).
- Correction: the Gemini key was available throughout v0.4 (`pico_campaign` loads it from `~/.bashrc`); the LLM experiments
  were not run, not blocked. Key verified with one metered request on 2026-10-31.
- The paper text moved to the Overleaf repo HumourousTree/Hardware-Fuzzing; `paper/` here holds only the generated tables,
  macros and figures (`tools/compare_literature.py --tex` checks the Overleaf copy for undefined macros).

# v0.6 (Rocket head-to-head on Encarsia's bugs)
- `rocket/`: BenchFuzz on Encarsia's 30 Rocket bugs (Cascade's tiny SoC, Spike, end-state oracle), compared per bug with the
  published DifuzzRTL / ProcessorFuzz / Cascade results. Details, problems and results: `docs/ROCKET_ENCARSIA.md`.
- Generator profiles `base` and `ext` (machine-mode CSRs, exceptions, compressed instructions); five implementation-defined
  Rocket/Spike differences neutralised; golden calibration before fuzzing; every detection replayed on the bug-free core.
- Results: base 7/15 mix-ups (= Cascade) and 3/15 conditionals; ext 13/15 mix-ups (6 detected by no published fuzzer) and
  4/15 conditionals. 133/150 detections confirmed; 16 rejected because the bug-free Rocket and Spike disagree after the FPU
  is switched off, 1 did not reproduce.
- `rocket/locate_bugs.py` (11/15 broken conditionals are in CSRFile), `rocket/confirm_detections.py`,
  `scripts/resume_background.sh` (restart unfinished long runs), `tests/test_rv64fuzz.py` (97 tests in total).
- Paper: new Sec. "Same bugs, same core" with Table `tab:rocket` and Fig. `fig:rocket`; abstract, contributions, threats and
  conclusion updated. A4 now reports 2 seeds (seed 2 running).

# v0.5 (LLM experiments on the free tier, oracle study, real-bug entry)
- Track A (`scripts/run_llm_experiments.sh`): certified LLM repair pass@k (A1), blind reading-vs-reasoning control (A2), LLM campaign
  with the legacy bugs (A3), ChipFuzzer-style generator after the plateau, capped at 100 requests (A4), stronger-model check (A5).
  `gemini-3.8-flash` returned 503 for ~5 h, so A1-A4 are pinned to `gemini-3.5-flash-lite`. A5 (`gemini-3.1-pro-preview`) was not
  run: the API reports a free-tier quota of 0 for `gemini-3.1-pro` on this key (requests per day and input tokens per minute).
- Results (14 observable bugs, 5 independent samples each, Yosys-certified):
  - A1 informed: certified pass@1 0.74, pass@5 0.79; feedback loop certifies 12/14 (template repair 7/14); 0 gate-accepted but
    uncertified LLM patches (template: 2/9). Unsolved: `branch_inversion_blt`, `stale_latched_branch`.
  - A2 blind: certified pass@1 0.40 (legacy 0.60 -> 0.52, plain 0.82 -> 0.33).
  - A3 campaign, 5 legacy bugs at once: all removed by iteration 10/200 (5 accepted patches, 20 repair requests); final RTL proven
    equivalent to golden; stimulus expansion 10/41 programs valid.
  - A4 target-line generator after the plateau (100 requests): 0/14 target attempts hit, all 7 targeted segments are
    configuration-dead or outside RV32IM; 64.24% vs 63.56% for mutation at 400 programs (1 seed).
- Fixes found while running Track A:
  - one `genai.Client` per key: google-genai closes its connection in `Client.__del__`, so a client per call left cached chats on
    a closed client (14 of 67 calls of the first A3 run failed; A3 was re-run, the first run is in `logs/archive/`).
  - a per-minute 429 waits for the API's retry hint and retries (max 3); only a daily or zero quota blocks the model for the day.
    `gemini-3.5-flash-lite` allows 15 RPM on this key, so its interval is 4.5 s. The quota metric is kept in `llm_calls.jsonl`.
  - `semantic_gen` counts only answered requests, and targets that got no request (allowance spent) separately (`no_llm`);
    the targeted line segments are recorded and classified like the unreached-line analysis.
  - A3 saves and certifies its final RTL; A5 is skipped (not retried for hours) when the strong model does not answer.
- Track B1 (`scripts/run_oracle_study.sh`): lockstep vs end-state oracle (`riscv_cosim.compare_end_state`, `benchmark --end-state`)
  paired on the same programs, 15 bugs x 10 seeds x 2 methods (random, mutation): identical detections (262/300 runs), end-state slower on 5 bugs.
- Real-bug benchmark entry `hw_design/real_bugs/picorv32_fence_rd` (+ `tools/check_real_bug.py`).
- Budget per model and per API key; transient 5xx retried with backoff; `repair_eval` resumable, model-pinned, private RTL copy,
  unanswered samples not scored, held-out runs skipped for textually identical candidates.
- `scripts/run_tracks.cmd`: resumable runner for all tracks (run in an open terminal if the session closes).
