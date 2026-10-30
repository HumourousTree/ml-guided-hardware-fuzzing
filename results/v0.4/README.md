# v0.4 evaluation results

Compact record of every experiment in the paper (Overleaf repo `HumourousTree/Hardware-Fuzzing`). `comparison_report.md` lists every number with its meaning;
`python tools/compare_literature.py --logs results/v0.4` regenerates the paper's tables and figures from this folder (except the
unique-coverage-point counts, which need the raw `logs/`).

| Path | Experiment |
|---|---|
| `experiments/experiment_e0_*.json` | E0 control on YosysHQ master, Cascade's `f00a88c` and the golden copy; mem_addr, Cascade-P6 and FENCE-rd probes |
| `experiments/experiment_e1_all.json`, `experiment_e2_*.json` | 15-bug suite (detection, minimisation, localisation) and concurrent injection |
| `bench/coverage/` | Coverage benchmark: random / mutation / hybrid, 7 seeds, 10,000 programs each |
| `bench/unified/`, `experiments/unified_coverage.json` | Unified co-coverage (line, toggle, reference statements, functional covergroups) |
| `bench/bugs/` | Time-to-bug: 15 bugs x 10 seeds x 2 methods, 1,000 programs |
| `bench/cascade_p6/`, `bench/fence_rd_master/` | Cascade P6 on `f00a88c`; FENCE with reserved rd on master |
| `experiments/hdd_eval.json` | Flat ddmin vs hierarchical DD on 80 fuzzer-found failures |
| `experiments/unreached_random.json` | RTL never covered, by region (classified in the paper) |
| `experiments/repair_baselines.json`, `repair.log`, `repair_patches/` | Repair-gate controls and template repair; accepted patches as diffs against golden |
| `experiments/certify.log`, `mem_handshake_unproven.json`, `equivalent_mutant_check.json` | Formal (Yosys) equivalence checks |
| `experiments/yosys_synth_xilinx_*.log` | PicoRV32 synthesis for Zynq UltraScale+ (`xcup`) and 7-series (`xc7`) |
| `bench/throughput/`, `bench/witness_hunt/` | Idle-machine throughput; fuzzing of the not-proven mem_handshake patch |
| `archive/runC_pico_campaign_telemetry.json` | Archived 552-iteration Gemini campaign (v0.3), per-iteration summary |

Benchmark JSONs keep every field except the raw coverage-point list (replaced by per-kind counts); time series are thinned to
log-spaced points plus every point where coverage changed.
