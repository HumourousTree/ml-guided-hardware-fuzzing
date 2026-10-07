# ML Guided Hardware Fuzzing

This workspace now runs a single simple dashboard-driven PicoRV32 campaign.

## What it does

- Runs a lockstep RV32I comparison loop using a local software reference.
- Uses algorithm fuzzing every n iterations (default n = 8) to mutate test programs without calling Gemini.
- If a comparison fails, it blindly requests a Gemini RTL rewrite for the mutable wrapper.
- If n = 1, Gemini is called every iteration.

## Files used by dashboard mode

- dashboard.py
- harness/pico_campaign.py
- harness/riscv_cosim.py
- sw_reference/golden_rv32i.py
- hw_design/picorv32_mutable.sv
- test_corpus/rv32i_programs.json

## Run

From WSL:

```bash
source venv/bin/activate
python3 -m pip install -r requirements.txt
streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```

From PowerShell:

```powershell
.\start_dashboard.ps1
```

## Dashboard controls

- Run continuously until stopped
- Time budget / max iterations
- Algorithm fuzz every n iterations (default 8)
- Seed
- Start / Stop / Refresh / Clear history and reset

## Notes

- Set GEMINI_API_KEY in your environment to enable Gemini calls.
- The mutable RTL file is hw_design/picorv32_mutable.sv.
- Telemetry is written to logs/pico_campaign_telemetry.json.
