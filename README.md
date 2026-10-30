# BenchFuzz — ML-guided differential fuzzing of the PicoRV32 RISC-V core (v0.4)

Lockstep RVFI comparison of Verilator-compiled PicoRV32 RTL against a pure-Python RV32IM reference model, with mutational /
random / LLM stimulus, delta debugging (flat and hierarchical), fault localisation, guarded block-patch repair and formal
certification of accepted patches. The v0.4 evaluation and the IEEE-format paper are in this repository.

## Repository layout

| Path | What it is |
|---|---|
| `harness/` | The loop: `riscv_cosim.py` (Verilator build, simulation, lockstep scoreboard, validator), `pico_campaign.py` (campaign + LLM repair), `benchmark.py` (generators and head-to-head runner), `delta_debugger.py` (slicing, ddmin, HDD, localiser), `template_repair.py` (repair controls, template baseline, formal equivalence), `run_experiments.py` (E0/E1/E2), `repair_eval.py` (pass@k), `asm.py`, `semantic_gen.py` |
| `sw_reference/` | `golden_rv32i.py` — the immutable RV32IM reference model |
| `hw_design/` | RTL: `picorv32_golden.sv` (= YosysHQ master `ef203c2`), `upstream_picorv32*.v` (+ `.commit` pins), `picorv32_mutable.sv` (working copy), bug manifests and injector |
| `tools/` | Analysis: `compare_literature.py` (all paper tables/figures/numbers), `hdd_eval.py`, `unified_coverage.py`, `uncovered_lines.py`, `certify_repairs.py`, `analyze_bench.py`, `package_results.py`, `fetch_upstream.py` |
| `scripts/` | Entry points: `run_full_evaluation.sh` (whole evaluation, WSL), `run_campaign.ps1/.cmd`, `start_dashboard.ps1/.cmd` (Windows → WSL) |
| `tests/` | 80 unit tests (`python3 -m unittest discover -s tests -t .`) |
| `docs/` | `BENCHMARK_PLAN.md` (runbook), `literature_baselines.json` (published numbers with sources) |
| `paper/` | `generated/` (macros + tables) and `figures/` for the paper, written by `tools/compare_literature.py`; the paper text lives in the Overleaf repo `HumourousTree/Hardware-Fuzzing` |
| `results/v0.4/` | Compact, committed results of every v0.4 experiment (`comparison_report.md` summarises them) |
| `test_corpus/` | Pinned seed programs for the campaign |
| `dashboard.py` | Streamlit dashboard for campaign telemetry |
| `logs/` | Raw local output (not committed): campaign telemetry, raw benchmark runs, `archive/` (Run C) |

## Quick start (inside WSL, project root)
```bash
source venv/bin/activate && python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -t .                 # 80 tests
python3 -m harness.run_experiments --experiment E0          # control on unmodified upstream: must be 100% matching
bash scripts/run_full_evaluation.sh 7                       # full v0.4 evaluation (hours)
python tools/compare_literature.py                          # regenerate paper/generated + paper/figures (needs matplotlib)
```
From Windows: `scripts\run_campaign.cmd [seconds] [iterations] [fuzz_every]` and `scripts\start_dashboard.cmd`.
Paper: edit in Overleaf (`HumourousTree/Hardware-Fuzzing`); after regenerating, copy `paper/generated` and `paper/figures` into its `report_v0.4/`.

## How a campaign works
1. Every iteration the corpus (pinned seeds + generated programs) is mutated locally and validated; every Nth iteration the LLM
   adds a program (seeded local fallback if the API fails).
2. Each program runs on the RTL and on the reference; retirements are compared in lockstep (missing/extra retirements fail).
3. On a mismatch: slice to a reproducer (state preload), ddmin it, localise candidate RTL blocks, ask the LLM for
   `// REPLACE LINES a-b` patches, lint, check the reproducer and growth prefixes, then accept only if the whole suite has no
   regression. `tools/certify_repairs.py` / `harness.template_repair.formal_equivalence` certify accepted patches formally.

## Environment
`GEMINI_API_KEY` (read from `~/.bashrc` if not set), `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `PICO_REPAIR_MODE=localized|monolithic`,
`PICO_GOLDEN_RECOVERY=1`.

LLM request budget (free tier, `harness/llm_budget.py`): `GEMINI_DAILY_LIMIT` (requests per model per day, default 15),
`GEMINI_MIN_INTERVAL_S` (default 10). A 429/quota error blocks that model until the quota resets (midnight US Pacific).
Usage: `python3 -m harness.llm_budget`; every request is logged in `logs/llm_calls.jsonl`.
Note: `/tmp` in WSL may be a small tmpfs; every tool deletes its Verilator work directories (~250 MB each).

History of changes and results: `CHANGES.md`.
