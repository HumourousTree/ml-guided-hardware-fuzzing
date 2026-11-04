#!/usr/bin/env bash
# Track B1: lockstep (every retirement) vs end-state (final registers + memory) oracle on the 15-bug suite, paired on the
# same programs: 10 seeds x {random, mutation} x 1000 programs per bug.  Resumable (finished runs are skipped).
#   bash scripts/run_oracle_study.sh [workers]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 9
source venv/bin/activate
W=${1:-4}; OUT=logs/bench/oracle; mkdir -p "$OUT"
BUGS=$(python3 -c "from hw_design.bug_injector import load_manifest; print(' '.join(load_manifest('all')))")
for b in $BUGS; do for s in $(seq 0 9); do
  [ -f "$OUT/mutation_bug-${b}_seed$s.json" ] && continue
  echo "--methods random mutation --seeds 1 --seed-start $s --bug $b --budget-programs 1000 --budget-seconds 86400 --end-state --out $OUT"
done; done | xargs -P "$W" -I{} sh -c 'python3 -m harness.benchmark {} >> logs/bench/oracle_jobs.log 2>&1'
echo "[$(date '+%F %T')] ORACLE STUDY DONE" | tee -a logs/bench/oracle_jobs.log
