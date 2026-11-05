#!/usr/bin/env bash
# Track A, second round (inside WSL, project root). Resumable: rerun to continue.  Progress: logs/llm_extra.log
#   A4b  target-line generator after the plateau on two more seeds (gemini-3.5-flash-lite, <=100 requests per seed)
#   A5   stronger model: the full A1 protocol (14 bugs, 5 independent samples + feedback loop, certified) on
#        gemini-3.8-flash.  gemini-3.1-pro has no free-tier quota on this key, and 3.8-flash has been returning 503/504,
#        so it is probed every PROBE_EVERY_S until STRONG_WAIT_S has passed.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 9
source venv/bin/activate
LOG=logs/llm_extra.log; mkdir -p logs/experiments
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
LITE=${LITE:-gemini-3.5-flash-lite}; STRONG=${STRONG:-gemini-3.8-flash}
STRONG_WAIT_S=${STRONG_WAIT_S:-172800}; PROBE_EVERY_S=${PROBE_EVERY_S:-1200}

probe() {
  python3 - "$1" <<'PY'
import sys
from harness import pico_campaign as pc
pc._TRANSIENT_BACKOFF_S = ()
client, _ = pc._get_gemini_client()
text, _, _ = pc._generate_with_gemini(client, [sys.argv[1]], "Reply with the single word OK.", purpose="availability_probe")
sys.exit(0 if text else 1)
PY
}

wait_for() {   # probe $1 every PROBE_EVERY_S until it answers or the deadline $2 (epoch s) passes
  until probe "$1"; do
    [ "$(date +%s)" -ge "$2" ] && return 1
    log "$1 unavailable (quota or outage), probing again in $PROBE_EVERY_S s"; sleep "$PROBE_EVERY_S"
  done
}

a4b() {
  local deadline=$(( $(date +%s) + STRONG_WAIT_S ))
  for s in 1 2; do
    if [ -f logs/bench/llm_hybrid/hybrid_seed$s.json ]; then
      python3 -c "import json,sys; sys.exit(1 if json.load(open('logs/bench/llm_hybrid/hybrid_seed$s.json')).get('llm_starved') else 0)" \
        && continue
      log "A4b seed $s was starved of LLM answers: archived and re-run"
      mkdir -p logs/archive/a4b_quota_starved && mv logs/bench/llm_hybrid/hybrid_seed$s.* logs/archive/a4b_quota_starved/
    fi
    wait_for "$LITE" "$deadline" || { log "A4b seed $s not run: $LITE unavailable"; return 1; }
    log "A4b seed $s"
    GEMINI_MODEL="$LITE" GEMINI_FALLBACK_MODEL="" python3 -m harness.benchmark --methods hybrid --seeds 1 --seed-start $s \
      --budget-programs 400 --budget-seconds 86400 --llm-calls 100 --save-programs --out logs/bench/llm_hybrid >> "$LOG" 2>&1
  done
  log "A4b done"
}

a5() {   # resumable: each stop (quota, outage) waits for the model and continues where it stopped, until the deadline
  local deadline=$(( $(date +%s) + STRONG_WAIT_S ))
  while true; do
    wait_for "$STRONG" "$deadline" || { log "A5 not finished: $STRONG unavailable until the deadline"; return 1; }
    log "A5 $STRONG: informed, 5 samples + feedback loop, all bugs"
    python3 -m harness.repair_eval --model "$STRONG" --mode informed --samples 5 --feedback-attempts 3 | tee -a "$LOG"
    [ "${PIPESTATUS[0]}" -eq 0 ] && { log "A5 done"; return 0; }
    log "A5 interrupted (model stopped answering); will resume"
  done
}

a4b & A4PID=$!
a5
wait $A4PID
log "EXTRA DONE"; python3 -m harness.llm_budget | tee -a "$LOG"
