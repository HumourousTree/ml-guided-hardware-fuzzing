#!/usr/bin/env bash
# Full v0.4 evaluation (no LLM needed), as run for the paper: controls, bug suite, coverage and time-to-bug benchmarks,
# Cascade P6 / FENCE-rd studies, H-DD, guarded-repair baselines with formal certification, unified coverage, throughput.
#   bash scripts/run_full_evaluation.sh [workers]        (inside WSL, from the project root, venv active)
# Budgets are in validated programs (testcases), not seconds, so results do not depend on machine load.
# /tmp may be a small tmpfs: every tool deletes its Verilator work dirs, but check `df -h /tmp` if a run stalls.
set -euo pipefail
W=${1:-7}
COV_PROGRAMS=${COV_PROGRAMS:-10000}
COV_SEEDS=${COV_SEEDS:-7}
BUG_PROGRAMS=${BUG_PROGRAMS:-1000}
SEEDS=${SEEDS:-10}
B="--budget-seconds 86400"
mkdir -p logs/bench/coverage logs/bench/bugs logs/experiments

python3 -m harness.run_experiments --experiment E0 --tag _upstream_master
python3 -m harness.run_experiments --experiment E0 --upstream hw_design/upstream_picorv32_f00a88c.v --tag _upstream_f00a88c
python3 -m harness.run_experiments --experiment E0 --upstream hw_design/picorv32_golden.sv --tag _golden
python3 -m harness.run_experiments --experiment E1 --suite all
python3 -m harness.run_experiments --experiment E2 --suite all --multibugs and_to_or_alu beq_inverted store_after_store_dropped
cp logs/experiments/experiment_e2.json logs/experiments/experiment_e2_v2trio.json
python3 -m harness.run_experiments --experiment E2 --suite all --multibugs fast_mul_rs2_signed_widened mem_addr_bit2_stuck \
    lui_auipc_rd_x0 stale_latched_branch store_after_store_dropped_intermittent
cp logs/experiments/experiment_e2.json logs/experiments/experiment_e2_v2five.json

BUGS=$(python3 -c "from hw_design.bug_injector import load_manifest; print(' '.join(load_manifest('all')))")
{
  for s in $(seq 0 $((COV_SEEDS - 1))); do
    echo "--methods random mutation hybrid-rand --seeds 1 --seed-start $s --budget-programs $COV_PROGRAMS $B --out logs/bench/coverage"
  done
  for s in 0 1 2; do
    echo "--methods random mutation hybrid-rand --seeds 1 --seed-start $s --budget-programs 3000 $B --save-programs --out logs/bench/unified"
  done
  for b in $BUGS; do for s in $(seq 0 $((SEEDS - 1))); do
    echo "--methods random mutation --seeds 1 --seed-start $s --bug $b --budget-programs $BUG_PROGRAMS $B --out logs/bench/bugs"
  done; done
  for s in $(seq 0 $((SEEDS - 1))); do
    echo "--methods random mutation --seeds 1 --seed-start $s --rtl hw_design/upstream_picorv32_f00a88c.v --fence-rate 0.02 --stop-on-mismatch --budget-programs 3000 $B --out logs/bench/cascade_p6"
    echo "--methods random mutation --seeds 1 --seed-start $s --rtl hw_design/upstream_picorv32.v --fence-rate 0.02 --stop-on-mismatch --budget-programs 3000 $B --out logs/bench/fence_rd_master"
  done
} | xargs -P "$W" -I{} sh -c 'python3 -m harness.benchmark {} >> logs/bench/jobs.log 2>&1'

python3 tools/unified_coverage.py logs/bench/unified --out logs/experiments/unified_coverage.json
python3 tools/hdd_eval.py logs/bench/bugs --out logs/experiments/hdd_eval.json --max-per-bug 6
python3 - <<'PY'          # one coverage file of the golden build = the universe of coverage points
import shutil
from pathlib import Path
from harness.riscv_cosim import build_and_simulate_program, get_space_safe_tempdir
from harness.pico_campaign import DEFAULT_SEED_PROGRAMS
w = get_space_safe_tempdir("universe-")
s = build_and_simulate_program(Path("hw_design/picorv32_golden.sv"), DEFAULT_SEED_PROGRAMS[0], w, generate_vcd=False)
shutil.copy(s["coverage_path"], "logs/experiments/universe_golden.coverage.dat"); shutil.rmtree(w)
PY
python3 tools/uncovered_lines.py logs/bench/coverage/random_seed*.json --universe-dat logs/experiments/universe_golden.coverage.dat \
    --rtl hw_design/picorv32_golden.sv --json-out logs/experiments/unreached_random.json > /dev/null
python3 -m harness.template_repair --budget 30          # guard controls + Ochiai template repair (private RTL copies)
python3 tools/certify_repairs.py --timeout 600           # Yosys whole-core equivalence + directed witnesses
python3 -m harness.benchmark --methods random mutation --seeds 1 --budget-programs 1000 $B --out logs/bench/throughput   # idle machine
python3 tools/package_results.py                         # compact copy in results/v0.4
echo "now on Windows or WSL with matplotlib: python tools/compare_literature.py   (writes paper/generated and paper/figures)"
