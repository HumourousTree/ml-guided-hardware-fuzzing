# BenchFuzz — sound differential fuzzing, certified repair and LLM-guided generation for RISC-V cores (v0.5)

Two parts:

* **PicoRV32** (`harness/`): lockstep RVFI comparison of Verilator-compiled RTL against a pure-Python RV32IM reference, with
  mutational / random / LLM stimulus, delta debugging (flat and hierarchical), fault localisation, guarded block-patch repair
  and formal certification of accepted patches.
* **Rocket and BOOM** (`rocket/`): BenchFuzz on the 60 injected bugs of the Encarsia benchmark, compared per bug with the
  published results of DifuzzRTL, ProcessorFuzz and Cascade: RV64 generator (random, hand-designed extension, LLM-written
  templates from measured ISA gaps), Spike end-state oracle, calibration of every bug-free reference and replay of every
  detection. Campaigns run locally or on GitHub Actions. Details: `docs/ROCKET_ENCARSIA.md`.

The paper text lives in the Overleaf repo `HumourousTree/Hardware-Fuzzing`; every number, table and figure is generated here.

## Repository layout

| Path | What it is |
|---|---|
| `harness/` | The loop: `riscv_cosim.py` (Verilator build, simulation, lockstep scoreboard, validator), `pico_campaign.py` (campaign + LLM repair), `benchmark.py` (generators and head-to-head runner), `delta_debugger.py` (slicing, ddmin, HDD, localiser), `template_repair.py` (repair controls, template baseline, formal equivalence), `run_experiments.py` (E0/E1/E2), `repair_eval.py` (pass@k), `asm.py`, `semantic_gen.py` |
| `sw_reference/` | `golden_rv32i.py` — the immutable RV32IM reference model |
| `hw_design/` | RTL: `picorv32_golden.sv` (= YosysHQ master `ef203c2`), `upstream_picorv32*.v` (+ `.commit` pins), `picorv32_mutable.sv` (working copy), bug manifests and injector |
| `rocket/` | Rocket/BOOM: `rv64fuzz.py` (generator profiles base / ext / llm / llmflash, Spike + Verilator runs), `run_encarsia.py` (builds, calibration, campaigns), `confirm_detections.py` (replay), `cloud_shard.py` + `merge_shards.py` (GitHub Actions), `isa_census.py` + `llm_templates.py` (LLM-guided generation), `locate_bugs.py`, `check_fs_states.py` / `check_frm.py` (core-deviation reproducers), `build_dut.sh`, `lower_amt.py`, `merge_dut.py` |
| `tools/` | Analysis: `compare_literature.py` (all paper tables/figures/numbers), `comparison_figures.py` (head-to-head, soundness, capability figures), `llm_figures.py`, `hdd_eval.py`, `unified_coverage.py`, `uncovered_lines.py`, `certify_repairs.py`, `analyze_bench.py`, `package_results.py`, `fetch_upstream.py` |
| `scripts/` | Entry points: `run_full_evaluation.sh` (whole evaluation, WSL), `run_llm_experiments.sh` + `run_llm_extra.sh` (LLM experiments A1-A5), `run_oracle_study.sh` (B1), `run_tracks.cmd` (Windows runner), `run_campaign.ps1/.cmd`, `start_dashboard.ps1/.cmd` (Windows → WSL) |
| `tests/` | 97 unit tests (`python3 -m unittest discover -s tests -t .`) |
| `docs/` | `EXPERIMENTS.md` (LLM and oracle experiments), `ROCKET_ENCARSIA.md` (Rocket/BOOM comparison, LLM-guided generation, core deviations), `BENCHMARK_PLAN.md` (runbook), `literature_baselines.json` and `encarsia_published.json` (published numbers with sources), `capabilities.json` (capability matrix with sources) |
| `docker/encarsia/`, `.github/workflows/` | Pinned toolchain image and the `encarsia-campaign.yml` workflow (up to 20 parallel shards; manual trigger, no secrets) |
| `paper/` | `generated/` (macros + tables) and `figures/` for the paper, written by `tools/compare_literature.py`; the paper text lives in the Overleaf repo `HumourousTree/Hardware-Fuzzing` |
| `results/v0.4/` | Compact, committed results of every experiment, including `bench/rocket` and `bench/boom` (`comparison_report.md` summarises them) |
| `test_corpus/` | Pinned seed programs for the campaign |
| `dashboard.py` | Streamlit dashboard for campaign telemetry |
| `logs/` | Raw local output (not committed): campaign telemetry, raw benchmark runs, `archive/` (Run C) |

## Quick start (inside WSL, project root)
```bash
source venv/bin/activate && python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -t .                 # 97 tests
python3 -m harness.run_experiments --experiment E0          # control on unmodified upstream: must be 100% matching
bash scripts/run_full_evaluation.sh 7                       # full v0.4 evaluation (hours)
python tools/compare_literature.py                          # regenerate paper/generated + paper/figures (needs matplotlib)
```
Rocket/BOOM (setup once with `bash scripts/setup_rocket_env.sh`):
```bash
python3 rocket/run_encarsia.py --core rocket --seeds 5 --programs 2000          # local campaign (also --core boom, --profile ext|llm|llmflash)
gh workflow run encarsia-campaign.yml --ref v0.4-evaluation -f core=boom -f profile=ext   # the same on GitHub Actions
python3 rocket/llm_templates.py --model gemini-3.5-flash-lite --rounds 5     # LLM-guided generator templates (needs the key)
```
Long local runs survive closed sessions: `bash scripts/resume_background.sh` restarts whatever is unfinished.
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

LLM request budget (free tier, `harness/llm_budget.py`): per model and per API key, limits in `MODEL_LIMITS` (set from the
limits the API reported: flash-lite 15 RPM / 500 RPD), override with `GEMINI_LIMITS="model:daily:interval_s,..."`; other models
use `GEMINI_DAILY_LIMIT` / `GEMINI_MIN_INTERVAL_S` (default 15 / 10 s). A per-minute 429 waits and retries; a daily or zero
quota blocks the model until midnight US Pacific. Usage: `python3 -m harness.llm_budget`; every request is logged in
`logs/llm_calls.jsonl`. Details and known problems: `docs/EXPERIMENTS.md`.
Note: `/tmp` in WSL may be a small tmpfs; every tool deletes its Verilator work directories (~250 MB each).

History of changes and results: `CHANGES.md`.
