# Gate mode vs quirk filter on BOOM (30 published Encarsia bugs, 5 seeds per bug and profile)

Replay-confirmed detections only (`tools/gate_compare.py`; the filter runs' replay rejections, e.g. 16 BOOM `llm`
detections where the bug-free BOOM disagrees with Spike, are not counted). All 290 gate-mode detections replay-confirmed.

| profile (budget/seed) | filter: bugs (mix/cond) | gate: bugs (mix/cond) |
|---|---|---|
| base (2,000) | 18 (12/6) | 19 (13/6) |
| ext (400) | 18 (12/6) | 19 (13/6) |
| LLM templates, llmflash (400) | 19 (12/7) | 20 (13/7) |
| union over profiles | 19 (12/7) | **20 (13/7)** |

* Gained: mix-up 3 (`driver/137`, `DivSqrtRecF64_mulAddZ31`, FP division/sqrt), 5/5 seeds in every profile (first
  detection after 16-515 base, 3-386 ext, 2-29 LLM-template programs). No published fuzzer finds it.
* Lost: nothing. The one filter-mode llmflash detection of `multiplexer/727` (seed 0) failed replay (not a bug detection).
* Cost: 2.4-2.8% of programs are skipped because the bug-free BOOM disagrees with Spike on them (counted as
  `reference_disagreements`; calibration mismatches on the bug-free cores: 135-210 of 7,500 per profile).
* Beyond all published fuzzers on BOOM: C4 (`multiplexer/160`) and M3 (`driver/137`).
