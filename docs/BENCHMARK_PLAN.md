# BenchFuzz evaluation runbook (v0.3)

All commands from the project root, inside WSL, venv active. Everything below is implemented and unit-tested against a
software DUT; **none of it has been run on Verilator yet** - run the steps in order and send back `logs/`.

## 0. Foundations
```bash
python3 tools/apply_fixes.py                       # if not already applied
python3 -m unittest discover -s tests -t .          # expect ~58 tests; repo-integrity ones need your RTL
python3 tools/fetch_upstream.py --commit f00a88c    # pristine YosysHQ source; f00a88c = the commit Cascade evaluated
python3 -m harness.run_experiments --experiment E0  # control + mem_addr convention probe (reads upstream_picorv32.v)
```
E0 passes if every run matches 100% **and** `convention_probe.conclusion` says CONVENTION GAP (upstream word-aligned `mem_addr`).

## 1. Bug suite (12 bugs, 3 tiers)
```bash
python3 -m harness.run_experiments --experiment E1 --suite all     # per bug: detection, ddmin, localisation hit, cycles
python3 -m harness.run_experiments --experiment E2 --suite all --multibugs and_to_or_alu beq_inverted store_after_store_dropped
```
Any bug reporting `RTL does not build` (edit is invalid Verilog) or `detected: false` (unobservable) must be fixed or reported as such.

## 2. Head-to-head benchmark (same DUT / validator / coverage / budget)
```bash
# no LLM needed
python3 -m harness.benchmark --methods random mutation --seeds 10 --budget-seconds 3600
# with an LLM (use a PAID key or a local model: free tier = 20 requests/day/model)
python3 -m harness.benchmark --methods random mutation semantic hybrid hybrid-rand --seeds 10 --budget-seconds 3600
# time-to-bug per method and bug (programs and seconds to first lockstep mismatch)
for b in and_to_or_alu beq_inverted store_after_store_dropped lui_auipc_rd_x0; do
  python3 -m harness.benchmark --methods random mutation --bug $b --seeds 10 --budget-seconds 600; done
python3 tools/analyze_bench.py logs/bench --out logs/bench_report      # tables (LaTeX), curves, Mann-Whitney, unique points
```
`hybrid` vs `hybrid-rand` vs `mutation` after the plateau is the "does the LLM help beyond the plateau" experiment
(plateau rule = ChipFuzzer's: < 0.1 pp gain over 100 validated testcases).

## 3. Repair
```bash
python3 -m harness.repair_eval --suite all --samples 5     # pass@1, pass@5 (independent samples), exact-fix rate, feedback-loop success
```

## 4. Unreached RTL (what limits coverage)
```bash
python3 tools/uncovered_lines.py /tmp/benchfuzz-picorv32-*/*.coverage.dat --rtl hw_design/picorv32_golden.sv
```

## Fairness checklist
Same validator, DUT build, wait-state setting, coverage definition, seeds `1000+k`/`2000+k`/`3000+k`; record the model name per run;
never label a re-implemented strategy with a published tool's name; report components (line, toggle), not only the unified mean.
