# ML Guided Hardware Fuzzing (v0.2, audited)

LLM-guided differential fuzzing, delta-debugging and block-patch repair of the PicoRV32 RISC-V core.
Lockstep RVFI comparison of Verilator-compiled RTL against a pure-Python RV32IM model.

## Quick start
```bash
source venv/bin/activate
python3 -m pip install -r requirements.txt
python3 tools/apply_fixes.py                   # one-off: restores upstream mem_la_addr, patches dashboard.py
python3 -m unittest discover -s tests -t .     # software-DUT tests (3 need the RTL files)
python3 -m harness.run_experiments --experiment E0   # control: upstream RTL must be 100% matching
streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```

## How a campaign works
1. Every iteration the corpus (3 pinned seeds + up to 3 generated programs) is mutated locally and validated.
   Every Nth iteration Gemini adds a program instead (seeded local fallback if the API fails).
2. Each program runs on the RTL and on the ISS; retirements are compared in lockstep. Missing/extra
   retirements count as failures.
3. On a genuine mismatch: slice to a reproducer (dynamic trace + state/memory preload), confirm it fails,
   ddmin it, localise candidate RTL blocks, ask Gemini for `// REPLACE LINES a-b` patches, lint, check the
   reproducer and growth prefixes, then accept only if the whole suite has no regression and fewer failures.

## Environment
`GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `PICO_REPAIR_MODE=localized|monolithic`,
`PICO_GOLDEN_RECOVERY=1` (allow restoring golden RTL on failure; reported separately, never a "repair").

Telemetry: `logs/pico_campaign_telemetry.json`. See `CHANGES.md` for the audit.
