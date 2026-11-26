# Track 1: a small generator trained on bug kills

Goal: a ~100M-parameter model, trained from scratch, that writes test-program bodies which expose bugs in Rocket and
BOOM, evaluated on bugs it never saw (the 60 published Encarsia bugs, then cross-core and real bugs). The LLM templates
of Step 2 (`docs/ROCKET_ENCARSIA.md`) showed that better stimulus finds bugs random stimulus misses; this track
replaces the API model with a small local one whose training signal is "this program killed this mutant".

## Pipeline

| Step | Code | Where | Status |
|---|---|---|---|
| 1. Mutants | `rocket/mutant_factory.py` | GitHub Actions (`mutant-factory.yml`) | Rocket/BOOM driver done; conditional pass after the `inject_amt.cc` fix |
| 2. Pool | `rocket/label_mutants.py pool` | GitHub Actions | 120 base + 120 ext + 120 LLM-template programs per core, gated |
| 3. Labels | `rocket/label_mutants.py label` | GitHub Actions (20 shards) | Rocket driver running |
| 4. Tokenizer | `rocket/isa_tokenizer.py` | anywhere | done, lossless |
| 5. Pretrain | `rocket/genmodel/{data,train}.py` | Kaggle (`rocket/genmodel/kaggle/`) | first run (small, then base) |
| 6. SFT + DPO on kills | `rocket/genmodel/train.py sft / dpo` | Kaggle | needs step 3 |
| 7. Evaluation | `rocket/genmodel/sample.py` + `label_mutants.py` | GitHub Actions | dev mutants, then held-out bugs |

## Rules that keep the result honest

* **Held out.** The published Encarsia bugs are never training mutants (excluded by directory and by identical
  netlist). The factory does not reproduce Encarsia's numbering, so the netlist check is the one that matters.
* **Dev split.** 15% of the sampled mutants are a dev split, used only to choose checkpoints.
* **Equivalent mutants.** Encarsia keeps only candidates that a commercial formal tool proves observable. We cannot,
  so duplicates are dropped by netlist hash and a mutant no pool program kills is reported as "unkilled, possibly
  equivalent", never as a bug and never as a DPO "winner" target.
* **No reward hacking.** A kill counts only for programs that pass the pool gate (Spike and the bug-free reference
  agree and both stop). A program cannot "kill" by hanging or by an implementation-defined difference.
* **Same harness.** The model writes only the body; `rv64fuzz.gen_program(body=...)` puts it in the same prologue,
  dump and trap handler as every other profile, with the same per-core quirk filters.

## Model and data

* Tokens are ISA fields (mnemonic, register, CSR, rounding mode, label, immediates as sign + hex digits): 601 tokens
  plus control tokens `ctl:core:*`, `ctl:prof:*`, `ctl:kill`. Bodies are 140-730 tokens.
* `small` = 6 x 384 (~11M), `base` = 12 x 768 (~85M), context 1024.
* Pretraining: 400k bodies from the base, ext and LLM-template generators for both cores (~120M tokens), packed
  windows. It teaches syntax and the harness rules; usability is measured by `sample.py` (decodes, passes the
  harness check, assembles).
* Fine-tuning: SFT on pool programs weighted by training-mutant kills, then DPO on (killer, non-killer) pairs.
