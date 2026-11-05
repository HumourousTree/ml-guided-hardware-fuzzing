# LLM and oracle experiments (v0.5): what each one measures, how to run it, where the results go

All runs use PicoRV32 at YosysHQ `ef203c2` (`hw_design/picorv32_golden.sv`), Verilator 5.032 and Yosys 0.52 in WSL.
Every number in the paper comes from `tools/compare_literature.py`, which reads `logs/` and writes `paper/generated/results.tex`
(macros), `paper/generated/tab_*.tex` (tables) and `paper/figures/` (figures). The paper text is in the Overleaf repository
`HumourousTree/Hardware-Fuzzing` (`report_v0.4/main.tex`); copy `paper/generated` and `paper/figures` there after regenerating.

## Overview

| ID | Question | Model | Entry point | Output (in `logs/`) | Paper (LaTeX labels, macro prefix) |
|---|---|---|---|---|---|
| A1 | How often does guarded LLM repair produce a *formally certified* fix? | gemini-3.5-flash-lite | `scripts/run_llm_experiments.sh` | `experiments/llm_repair_informed_<model>.json` | `tab:llmrepair`, `fig:llmrepair` (`Llm*`) |
| A2 | Does the model use the failure, or only read the code? (blind control) | same | same | `experiments/llm_repair_blind_<model>.json` | "Reading or reasoning?" (`LlmBlind*`) |
| A3 | Does the closed loop remove several bugs at once without golden recovery? | same | same | `experiments/llm_campaign/` | `fig:llmcampaign` (`Camp*`) |
| A4 | Does ChipFuzzer-style target-line prompting help after the plateau? | same | same, seeds 1-2: `scripts/run_llm_extra.sh` | `bench/llm_hybrid/hybrid_seed*.json` | `fig:llmhybrid` (`Hyb*`) |
| A5 | Does a stronger model change A1? | gemini-3.8-flash | `scripts/run_llm_extra.sh` | `experiments/llm_repair_informed_gemini-3.8-flash.json` | `tab:llmrepair` last column (`LlmStrong*`) |
| B1 | What does checking every retirement add over an end-state check? | none | `scripts/run_oracle_study.sh` | `bench/oracle/` | `tab:oracle`, `fig:oracle` (`Orc*`) |
| RB | Real upstream bug (FENCE writes its reserved `rd`) | none | `python3 tools/check_real_bug.py` | `hw_design/real_bugs/picorv32_fence_rd/verification.json` | "A real-bug benchmark entry" (`Rb*`) |

Run everything inside WSL from the project root with `source venv/bin/activate`. Every runner is resumable: rerun it and it
continues where it stopped (finished bugs, samples and seeds are skipped).

## Protocol details

**A1 (informed repair).** For each of the 14 observable bugs (`hw_design/bug_manifest*.json`; `fast_mul_decode_and_to_or` is
equivalent on all valid programs and is skipped) `harness/repair_eval.py` draws 5 *independent* samples. Each sample has a
fresh model session, an empty rejection memory and one attempt. It goes through the unchanged pipeline: slice, ddmin, localise,
block patch, lint, reproducer, growth prefixes, whole regression suite at memory delays 0 and 1. Every gate-accepted patch is then
run on 200 held-out programs at delays 0 and 3, and certified by Yosys whole-core equivalence against the golden RTL.
pass@k uses the unbiased estimator `1 - C(n-c,k)/C(n,k)`. One extra run per bug uses the guarded loop with 3 attempts and
feedback. It is reported separately because its attempts are not independent.
The model is pinned (`GEMINI_FALLBACK_MODEL=""`), golden recovery is off, and a sample in which the model never answered is
not scored (the run stops with exit code 3 and resumes later).

**A2 (blind control).** The same as A1, but `blind_prompt()` removes the failing instruction, reproducer, discrepancy
signature and per-block diagnoses, and states only that one of the candidate blocks contains an injected defect.
The comparison is split into the 5 *legacy* bugs (`bug_manifest.json`, conspicuous constants) and the 9 *plain* mutations.

**A3 (closed-loop campaign).** The 5 legacy bugs are injected together into `hw_design/picorv32_mutable.sv`. Then
`harness.pico_campaign` runs 200 iterations, with LLM stimulus expansion every 8th iteration and a repair attempt on every
failing comparison. The final RTL is saved (`final_rtl.sv`), certified against golden (`final_rtl_formal.json`), and the golden
RTL is restored.

**A4 (target-line generator after the plateau).** `benchmark --methods hybrid --llm-calls 100`: mutation until ChipFuzzer's
plateau rule fires, then `harness/semantic_gen.py`. It picks an uncovered line segment, retrieves the closest seed, asks for an
assembly test, and gives up to 5 syntax and 5 semantic correction rounds. After 100 answered requests per seed it falls back to
mutation. The run is paired with the mutation and mutation-then-random runs of the same seed (`bench/coverage/`). Each targeted
segment is classified with the rules of the unreached-line analysis (`UNREACHED_RULES`). A run in which the model stopped
answering before the cap is flagged `llm_starved` and excluded.

**A5 (stronger model).** The A1 protocol on `gemini-3.8-flash`. `gemini-3.1-pro` was the first choice, but the API reports a
free-tier quota of 0 for it on this key. `run_llm_extra.sh` probes the model every 20 min for up to 48 h (it was returning 503/504
on 2026-11-04/05) and resumes after every interruption.

**B1 (oracle study).** 15 bugs x 10 seeds x {random, mutation} x 1000 programs, with the lockstep oracle and the end-state
oracle (`riscv_cosim.compare_end_state`: final registers and every written memory byte, rebuilt from the RTL's retirement
records) evaluated on the same programs. Each run continues after the lockstep detection until the end-state oracle fires too.

## Results (2026-11-05)

| | Result |
|---|---|
| A1 | certified pass@1 0.74, pass@5 0.79 (14 bugs, 70 samples, 87 requests); loop certifies 12/14; template repair 7/14; **0** gate-accepted but uncertified LLM patches (template: 2/9) |
| A2 | certified pass@1 0.40; legacy 0.60 -> 0.52, plain 0.82 -> 0.33; `beq_inverted` better blind (1.0 vs 0.4) |
| A3 | all 5 bugs removed by iteration 10/200; 5 accepted patches, 1 rollback, 20 repair requests, 0 golden recoveries; final RTL **proven** equivalent (not textually identical); expansion 10/41 valid |
| A4 | seed 0: 100 requests, 14 target attempts on 7 segments, 0 hits. All 7 segments are configuration-dead or outside RV32IM. 64.24% vs 63.56% (mutation) and 63.39% (mutation then random) at 400 programs. Seeds 1-2 pending (quota) |
| A5 | pending: gemini-3.8-flash unavailable (503/504) |
| B1 | 262/300 detections for both oracles; 0 bugs found only by lockstep; end-state slower on 5 bugs (e.g. `fast_mul_rs2_signed_widened` 16 vs 35 programs) |
| RB | 31/31 directed cases diverge on upstream, 0/31 on the fixed core; fixed core passes E0 150/150 and 18,000 fuzzing programs |

## Free-tier budget (`harness/llm_budget.py`)

Limits are the ones the API reported for this key in its 429 replies, not the advertised ones:

| Model | Reported limit | Budget used | Note |
|---|---|---|---|
| gemini-3.5-flash-lite | 15 RPM, **500 RPD** | 4.5 s interval, 400/day | the advertised 1500 RPD did not hold |
| gemini-3.8-flash | not yet observed | 5 s interval, 400/day | 503/504 outage since 2026-11-04 |
| gemini-3.1-pro | **0** | - | no free-tier quota on this key |

A per-minute 429 waits for the API's retry hint and retries (max 3). A daily or zero-quota 429 blocks the model until
midnight US Pacific. Transient 5xx and timeouts are retried after 15/45/120/240 s. Every request is logged in
`logs/llm_calls.jsonl` (time, model, purpose, sizes, outcome, the quota metric for 429s; the key only as a hash).
Status: `python3 -m harness.llm_budget`.

## Things that went wrong, and how they were handled

| Problem | Effect | Fix |
|---|---|---|
| gemini-3.8-flash 503/504 for hours | A1-A4 could not use it | model chosen once by `PRIMARY_WAIT_S`; A1-A4 pinned to flash-lite; A5 waits for 3.8-flash |
| `genai.Client` closed in `__del__` while cached chats still used it | 14/67 calls of the first A3 run failed ("client has been closed") | one client per key (`_GEMINI_CLIENT`); A3 re-run; first run in `logs/archive/run_20261105_closed_client/` |
| per-minute 429 treated as daily | first A4 run stopped early | `quota_kind()` separates minute from day |
| generator counted refused requests as calls and refused targets as give-ups | first A4 run reported 217 calls / 129 targets for 100 real requests | `semantic_gen` counts answered requests only; `no_llm` for targets without a request; A4 re-run |
| flash-lite daily limit is 500, not 1500 | A4 seeds 1-2 got 5 answers | runs flagged `llm_starved` and excluded; caps lowered to 400/day; seeds re-run after the reset (archived in `logs/archive/a4b_quota_starved/`) |
| gemini-3.1-pro has 0 free-tier quota | A5 impossible on this key | A5 on gemini-3.8-flash; skipped (not retried for hours) when a model has no quota |
| WSL shuts down when no client is attached | background jobs died with the session | run from an open session, or `scripts\run_tracks.cmd` in an open terminal |

## Figures (`tools/llm_figures.py`)

| File | Shows |
|---|---|
| `fig_llm_repair` | per-bug certified pass@1, informed (filled) vs blind (open), stronger model (diamond) once A5 exists; loop and template columns; legacy/plain split |
| `fig_llm_campaign` | A3: bug-targeted programs failing before each repair attempt, and the attempt's outcome |
| `fig_oracle` | B1: median programs to detection, lockstep vs end-state, per bug (log-log, diagonal = equal) |
| `fig_llm_hybrid` | A4: coverage after the plateau, LLM generator vs mutation vs mutation-then-random on the same seeds |

All figures use one colour-blind-safe palette (Okabe-Ito), are saved as PDF (paper) and PNG (preview) with a tight bounding box,
and are skipped silently when their data is missing.

## Interruptions and resuming

All long runs keep their results on disk as they go and skip finished work when rerun: A4b one file per seed
(`logs/bench/llm_hybrid/`; a seed starved of LLM answers is archived and rerun), A5 one result per bug, Rocket one file
per bug and seed (`logs/bench/rocket/`). Only the item in progress at the interruption starts over. LLM requests and the
daily budget are logged in `logs/llm_calls.jsonl` / `logs/llm_budget.json`, so a restart does not reset the request cap.
After a closed session, reboot or WSL restart run `bash scripts/resume_background.sh` (inside WSL), which restarts only
unfinished, not-running jobs; `bash scripts/resume_background.sh status` shows progress.

## Reproducing the paper numbers

```bash
bash scripts/run_oracle_study.sh            # B1 (no LLM, ~hours)
bash scripts/run_llm_experiments.sh         # A1-A4 (free tier; resumable)
bash scripts/run_llm_extra.sh               # A4 seeds 1-2, A5 (waits for quota / availability)
python3 tools/check_real_bug.py             # RB
python tools/compare_literature.py          # macros, tables, figures; prints macros the paper uses but nobody generates
python tools/package_results.py             # compact copy in results/v0.4 (committed)
```
