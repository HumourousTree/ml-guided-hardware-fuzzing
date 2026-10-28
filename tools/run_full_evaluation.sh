#!/usr/bin/env bash
# Full v0.4 evaluation (no LLM needed): controls, bug suite, head-to-head coverage benchmark, time-to-bug.
#   bash tools/run_full_evaluation.sh [workers]        (inside WSL, from the project root, venv active)
# Budgets are in validated programs (testcases), not seconds, so results do not depend on machine load.
set -euo pipefail
W=${1:-7}
COV_PROGRAMS=${COV_PROGRAMS:-10000}
BUG_PROGRAMS=${BUG_PROGRAMS:-3000}
SEEDS=${SEEDS:-10}
mkdir -p logs/bench/coverage logs/bench/bugs logs/experiments

python3 -m harness.run_experiments --experiment E0 --tag _upstream_master
python3 -m harness.run_experiments --experiment E0 --upstream hw_design/upstream_picorv32_f00a88c.v --tag _upstream_f00a88c
python3 -m harness.run_experiments --experiment E1 --suite all
python3 -m harness.run_experiments --experiment E2 --suite all --multibugs and_to_or_alu beq_inverted store_after_store_dropped

BUGS=$(python3 -c "from hw_design.bug_injector import load_manifest; print(' '.join(load_manifest('all')))")
{
  for s in $(seq 0 $((SEEDS - 1))); do
    echo "--methods random mutation hybrid-rand --seeds 1 --seed-start $s --budget-programs $COV_PROGRAMS --budget-seconds 86400 --out logs/bench/coverage"
  done
  for b in $BUGS; do for s in $(seq 0 $((SEEDS - 1))); do
    echo "--methods random mutation --seeds 1 --seed-start $s --bug $b --budget-programs $BUG_PROGRAMS --budget-seconds 86400 --out logs/bench/bugs"
  done; done
} | xargs -P "$W" -I{} sh -c 'python3 -m harness.benchmark {} >> logs/bench/jobs.log 2>&1'

python3 tools/analyze_bench.py logs/bench/coverage --out logs/bench_report
python3 tools/hdd_eval.py logs/bench/bugs --out logs/experiments/hdd_eval.json
