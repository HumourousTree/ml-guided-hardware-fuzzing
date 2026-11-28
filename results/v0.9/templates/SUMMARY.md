# Module-targeted LLM templates (2026-11-29)

**Question.** Does telling the template LLM which modules still hide bugs help it find more bugs?

**Setup.**
- `llmtgt` (`rocket/templates/llm_target.json`, 26 templates):
  - gemini-3.6-flash, 3 requests.
  - The prompt lists the modules holding the most training-split mutants that the existing profiles never killed, with their signal names (`llm_templates.py --target`).
  - Rocket targets: CSRFile, Rocket, MulDiv, RVCExpander, BreakpointUnit, IBuf.
  - BOOM targets: Rob, CSRFile, branch queues, FP conversion units.
- `llmctl` (`llm_census36.json`, 12 templates) is the control:
  - same model, same budget;
  - the original instruction-coverage prompt.

Both profiles ran through the same pipeline and the same quirk filters.

## Published bugs (replay-confirmed, 5 seeds; `tools/profile_compare.py`)

| core | base | ext | llmflash | llmctl | llmtgt |
|---|---|---|---|---|---|
| Rocket | 10 | 17 | 16 | 16 | 14 |
| BOOM | 18 | – | 19 | 20 | 19 |

- **Rocket:** `llmtgt` misses M8 (driver 774) and M9 (driver 804), which every other LLM profile finds (`rocket_compare.txt`).
- **BOOM:** `llmctl` finds M3 (driver 137) on 1 of 5 seeds. No other filter-mode profile finds it (`boom_compare.txt`).

**BOOM `llmtgt` calibration.** The first `llmtgt` campaign on BOOM failed calibration on all 15 references.
- Three templates wrote `frm` from a register, which can set an invalid rounding mode.
- BOOM does not trap on that rounding mode; Spike does.
- `frm_safe` (commit 3997eae) applies the generator's valid-`frm` rule to template lines.
- Programs of the other profiles are byte-identical.
- The relaunch (run 46138529808) calibrated.

## Mutant kills per module (`labels/`, `tools/template_scripts/target_kills.py`)

**Setup.**
- label-mutants pool `llmtgt:120,llmctl:120`, 400 sampled mutants per core and kind.
- A mutant counts as killed by a profile if any of its 120 programs kills it.
- The targets were chosen from training-split mutants, so the dev split is held out.

| core / kind | split | mutants in targeted modules | llmtgt | llmctl | all mutants: llmtgt | all mutants: llmctl |
|---|---|---|---|---|---|---|
| Rocket driver | dev | 55 | 22 | 22 | 27 | 27 |
| Rocket mux | dev | 58 | 15 | 15 | 15 | 15 |
| BOOM driver | dev | 4 | 2 | 1 | 31 | 30 |
| BOOM mux | dev | 26 | 3 | 3 | 11 | 10 |
| Rocket driver | train | 294 | 98 | 101 | 140 | 143 |
| Rocket mux | train | 334 | 79 | 83 | 79 | 83 |
| BOOM driver | train | 27 | 7 | 6 | 172 | 166 |
| BOOM mux | train | 159 | 17 | 16 | 52 | 51 |

On held-out mutants in the targeted modules, `llmtgt` kills 42 and `llmctl` 41.

The per-module trade-offs are small and cancel out:
- **Rocket core module (mux, train):** `llmtgt` kills 7 more (20 vs 13).
- **RVCExpander:** `llmtgt` kills 18 fewer (15 vs 21 driver, 0 vs 12 mux). The control writes many compressed-instruction templates.
- **Modules both profiles fail on:**
  - BreakpointUnit: 1 of 40 train, 1 of 10 dev, for both.
  - BOOM Rob: 12 vs 13 of 83.
  - BOOM BranchKillableQueue_3: 0 for both.

No `llmtgt` template writes `tselect`/`tdata`, so the LLM did not act on the BreakpointUnit target.

## Conclusion

Naming target modules and signals in the prompt does not help (negative result).

The LLM turns a module name into instructions that already reach that module. It does not produce the configuration (debug triggers, ROB-full or branch-kill timing) that the surviving mutants need.

Next steps that would address this:
- a reward on activation of the mutated signal (activation/propagation experiment);
- hand-written or formally derived configuration prefixes (#7, BMC witnesses).
