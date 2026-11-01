# BenchFuzz evaluation runbook (v0.4 — executed)

Everything below was run on Verilator 5.032 / Yosys 0.52 (WSL2 Ubuntu, 8 cores) for the v0.4 paper. Results: `results/v0.4/`
(compact) and `logs/` (raw, not committed). One command reproduces the whole evaluation:

```bash
bash scripts/run_full_evaluation.sh 7        # inside WSL, project root, venv active; ~6-8 h on 8 cores
python tools/compare_literature.py         # needs matplotlib; writes paper/generated/*.tex and paper/figures/*.pdf
```

| Step | Command (in the script) | Output |
|---|---|---|
| E0 controls (master, f00a88c, golden) + mem_addr, Cascade-P6 and FENCE-rd probes | `run_experiments --experiment E0 [--upstream ..] --tag ..` | `experiment_e0_*.json` |
| E1 15-bug suite, E2 concurrent injection | `run_experiments --experiment E1/E2 --suite all` | `experiment_e1_all.json`, `experiment_e2_*.json` |
| Coverage benchmark (7 seeds x random / mutation / hybrid-rand, 10k programs) | `benchmark --budget-programs 10000` | `logs/bench/coverage` |
| Unified coverage (3 seeds, programs saved) | `benchmark --save-programs` + `tools/unified_coverage.py` | `unified_coverage.json` |
| Time-to-bug (15 bugs x 10 seeds x 2 methods, 1000 programs) | `benchmark --bug <id>` | `logs/bench/bugs` |
| Cascade P6 on f00a88c / FENCE-rd on master (10 seeds, fence 2%) | `benchmark --fence-rate 0.02 --stop-on-mismatch` | `cascade_p6`, `fence_rd_master` |
| Delta debugging (flat ddmin vs HDD) on fuzzer failures | `tools/hdd_eval.py` | `hdd_eval.json` |
| Unreached RTL classification | `tools/uncovered_lines.py --universe-dat` | `unreached_random.json` |
| Repair: guard controls + Ochiai template baseline | `harness.template_repair --budget 30` | `repair_baselines.json` |
| Formal certification of accepted patches | `tools/certify_repairs.py --timeout 600` | adds `formal`, `witness` |
| Idle-machine throughput | `benchmark --budget-programs 1000` | `logs/bench/throughput` |

## Still to run (needs an LLM)
```bash
export GEMINI_API_KEY=...                  # paid tier or a local model; record the model name per run
python3 -m harness.benchmark --methods semantic hybrid --seeds 7 --budget-programs 10000 --budget-seconds 86400 --out logs/bench/coverage
python3 -m harness.repair_eval --suite all --samples 5          # pass@1 / pass@5 with independent samples
```
`repair_eval.py` reports pass@k twice: by the gates (`pass@k`) and certified by Yosys whole-core equivalence (`pass@k_certified`).
Report the certified figure: v0.4 found two gate-passing, held-out-passing template patches that are not correct.
`hybrid` vs `hybrid-rand` vs `mutation` after the plateau is the "does the LLM help beyond the plateau" experiment. Note from v0.4:
on PicoRV32 the ChipFuzzer plateau rule fires after ~200 programs although mutation is still gaining (Sec. RQ2 of the paper).

## Fairness checklist
Same validator, DUT build, wait-state setting, coverage definition, seeds `1000+k`/`2000+k`/`3000+k`; budgets in programs;
never label a re-implemented strategy with a published tool's name; report components (line, toggle), not only the unified mean;
certify accepted patches formally, not only with test suites.
