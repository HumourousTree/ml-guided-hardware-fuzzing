#!/usr/bin/env bash
# All remaining v0.5 experiment work, resumable: Track A (LLM) and Track B1 (oracle study) in parallel, then the FENCE-rd
# real-bug verification.  Rerunning continues where it stopped.  Progress: logs/tracks.log
cd "$(dirname "$0")/.." || exit 9
log() { echo "[$(date '+%F %T')] $*" >> logs/tracks.log; }
log "tracks start (pid $$)"
bash scripts/run_llm_experiments.sh > logs/llm_track.stdout 2>&1 &
A=$!
bash scripts/run_oracle_study.sh 4 > logs/oracle_track.stdout 2>&1
log "oracle study finished"
if [ ! -s hw_design/real_bugs/picorv32_fence_rd/verification.json ]; then
  rm -rf logs/bench/fence_rd_fixed
  (source venv/bin/activate && python3 tools/check_real_bug.py --seeds 3 --programs 3000 > logs/real_bug_check.log 2>&1)
  log "real-bug verification finished"
fi
wait $A
log "LLM track finished (exit $?)"
log "ALL TRACKS DONE"
