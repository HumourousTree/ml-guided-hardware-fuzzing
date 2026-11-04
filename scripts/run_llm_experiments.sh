#!/usr/bin/env bash
# Track A: LLM experiments on the free-tier Gemini key (inside WSL, project root). Resumable: rerun to continue.
#   bash scripts/run_llm_experiments.sh            progress: logs/llm_track.log, requests: python3 -m harness.llm_budget
#
# One model is chosen ONCE for A1-A4 so every comparison (LLM vs template repair, informed vs blind, campaign) uses the same
# model: gemini-3.8-flash if it answers within PRIMARY_WAIT_S, otherwise gemini-3.5-flash-lite.  A5 uses gemini-3.1-pro-preview.
# Budget limits come from harness/llm_budget.py (per model, per key); a run that gets no answer stops and is retried here.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 9
source venv/bin/activate
LOG=logs/llm_track.log; mkdir -p logs/experiments
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
PRIMARY=${PRIMARY:-gemini-3.8-flash}; SECONDARY=${SECONDARY:-gemini-3.5-flash-lite}; STRONG=${STRONG:-gemini-3.1-pro-preview}
PRIMARY_WAIT_S=${PRIMARY_WAIT_S:-3600}
LEGACY="approx_multiplier alu_operator_xor_or branch_inversion_blt writeback_forwarding_drop mem_handshake_drop"
PLAIN="and_to_or_alu fast_mul_rs2_signed_widened mem_addr_bit2_stuck slt_signed_as_unsigned beq_inverted lui_auipc_rd_x0"

probe() {   # 0 if the model answers a one-word prompt (costs one metered request)
  python3 - "$1" <<'PY'
import sys
from harness import pico_campaign as pc
pc._TRANSIENT_BACKOFF_S = ()
client, _ = pc._get_gemini_client()
text, _, _ = pc._generate_with_gemini(client, [sys.argv[1]], "Reply with the single word OK.", purpose="availability_probe")
sys.exit(0 if text else 1)
PY
}

retry() {   # run "$@" until it exits 0; exit code 3 = model unavailable -> wait and resume
  local n=0
  while true; do
    "$@"; local rc=$?
    [ $rc -eq 0 ] && return 0
    n=$((n + 1)); [ $n -gt 18 ] && { log "giving up after $n attempts: $*"; return 1; }
    log "exit $rc, retrying in 10 min (attempt $n): $*"; sleep 600
  done
}

MODEL_FILE=logs/experiments/llm_track_model.txt
if [ -s "$MODEL_FILE" ]; then MODEL=$(cat "$MODEL_FILE"); log "resuming with model $MODEL"
else
  MODEL=""; deadline=$(( $(date +%s) + PRIMARY_WAIT_S ))
  while [ -z "$MODEL" ]; do
    if probe "$PRIMARY"; then MODEL=$PRIMARY
    elif [ "$(date +%s)" -ge "$deadline" ]; then
      probe "$SECONDARY" && MODEL=$SECONDARY || { log "neither model answers; retry later"; exit 1; }
    else log "$PRIMARY unavailable, probing again in 10 min"; sleep 600; fi
  done
  echo "$MODEL" > "$MODEL_FILE"; log "model for A1-A4: $MODEL"
fi

log "A1 LLM repair (informed, 5 independent samples + feedback loop, certified)"
retry python3 -m harness.repair_eval --model "$MODEL" --mode informed --samples 5 --feedback-attempts 3 | tee -a "$LOG"

log "A2 reading-vs-reasoning control (blind prompt, 5 samples)"
retry python3 -m harness.repair_eval --model "$MODEL" --mode blind --samples 5 | tee -a "$LOG"

if [ ! -f logs/experiments/llm_campaign_done ]; then
  log "A3 LLM campaign with the five legacy bugs (dashboard: logs/pico_campaign_telemetry.json)"
  python3 -c "from hw_design.bug_injector import inject_multiple; assert inject_multiple('$LEGACY'.split(), 'legacy')"
  GEMINI_MODEL="$MODEL" GEMINI_FALLBACK_MODEL="" python3 -m harness.pico_campaign --seconds 86400 --iterations 200 \
      --seed 20260825 --fuzz-every 8 >> logs/llm_campaign.log 2>&1
  mkdir -p logs/experiments/llm_campaign && cp logs/pico_campaign_telemetry.json logs/experiments/llm_campaign/telemetry.json
  python3 -c "from hw_design.bug_injector import restore_golden; restore_golden()"
  touch logs/experiments/llm_campaign_done
fi

if [ ! -f logs/bench/llm_hybrid/hybrid_seed0.json ]; then
  log "A4 ChipFuzzer-style generator after the plateau (1 seed, at most 100 requests)"
  GEMINI_MODEL="$MODEL" GEMINI_FALLBACK_MODEL="" python3 -m harness.benchmark --methods hybrid --seeds 1 \
      --budget-programs 400 --budget-seconds 86400 --llm-calls 100 --save-programs --out logs/bench/llm_hybrid >> "$LOG" 2>&1
fi

log "A5 stronger model ($STRONG) on the plain-mutation bugs (5 samples, no feedback loop)"
retry python3 -m harness.repair_eval --model "$STRONG" --mode informed --samples 5 --feedback-attempts 1 --bugs $PLAIN | tee -a "$LOG"

log "TRACK A DONE"; python3 -m harness.llm_budget | tee -a "$LOG"
