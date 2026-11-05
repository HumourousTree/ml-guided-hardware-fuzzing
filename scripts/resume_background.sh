#!/usr/bin/env bash
# Restart the long-running jobs after a closed session, a reboot or a WSL restart (inside WSL, any directory).
# Both jobs skip finished work: Rocket keeps one result file per bug and seed (logs/bench/rocket/), the LLM runner
# keeps its per-seed / per-bug results and continues where it stopped. A run that was in progress starts again.
# Jobs that are already running or already finished are left alone. Progress: logs/rocket_encarsia.log, logs/llm_extra.log
#   From Windows, detached from any terminal:
#   Start-Process wsl -WindowStyle Hidden -ArgumentList '-d Ubuntu -- bash "/mnt/c/.../scripts/resume_background.sh"'
cd "$(dirname "$0")/.." || exit 9
mode=${1:-start}
if [ "$mode" = status ]; then
  pgrep -af "run_encarsia.py|scripts/run_llm_extra.sh" | grep -v pgrep | cut -c1-120
  echo "rocket: $(ls logs/bench/rocket/*_seed?.json 2>/dev/null | wc -l)/150 runs, ext profile: $(ls logs/bench/rocket/*_seed?_ext.json 2>/dev/null | wc -l)/150"; tail -1 logs/llm_extra.log; exit 0
fi
if ! pgrep -f "run_encarsia.py --seeds" >/dev/null && ! grep -q '^exit 0' logs/rocket_encarsia.log 2>/dev/null; then
  echo "[$(date '+%F %T')] resume_background: (re)starting the Rocket campaign" >> logs/rocket_encarsia.log
  ( python3 rocket/run_encarsia.py --seeds 5 --programs 2000 --jobs 6 --build-jobs 2 >> logs/rocket_encarsia.log 2>&1
    echo "exit $?" >> logs/rocket_encarsia.log ) < /dev/null &
fi
if ! pgrep -f "run_encarsia.py --profile ext" >/dev/null && ! grep -q '^exit 0' logs/rocket_encarsia_ext.log 2>/dev/null; then
  echo "[$(date '+%F %T')] resume_background: (re)starting the Rocket ext-profile campaign" >> logs/rocket_encarsia_ext.log
  ( python3 rocket/run_encarsia.py --profile ext --seeds 5 --programs 400 --jobs 6 --build-jobs 2       >> logs/rocket_encarsia_ext.log 2>&1; echo "exit $?" >> logs/rocket_encarsia_ext.log ) < /dev/null &
fi
if ! pgrep -f "scripts/run_llm_extra.sh" >/dev/null && ! grep -q "EXTRA DONE" logs/llm_extra.log 2>/dev/null; then
  echo "[$(date '+%F %T')] resume_background: (re)starting the LLM runner" >> logs/llm_extra.log
  bash scripts/run_llm_extra.sh >> logs/llm_extra.stdout 2>&1 < /dev/null &
fi
wait   # keeps this wsl.exe (and so the WSL VM) alive until both jobs end
