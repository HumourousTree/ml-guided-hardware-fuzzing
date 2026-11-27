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

## Decoding: grammar-constrained, cached

The model never has to "learn" what the assembler will reject. `rocket/genmodel/grammar.py` derives an ISA grammar
from the existing generators, separately for each profile:

* the operand signature of every mnemonic (register, FP register, CSR, rounding mode, immediate, memory, label);
* for every immediate field, the range and alignment seen, widened to the ISA's legal range where the generator draws
  it at random, with a hex-digit lookahead so that a value can always be completed (no dead ends);
* memory operands: the base registers seen (in practice `x30`, the data region) and offsets inside the region;
* compressed instructions only while `.option rvc` is in force (tracked through `.option push/pop`), with their
  register sets (x8-x15 for most);
* the harness rules: never write `x30`/`x31` (or `x29` with a trap handler), labels referenced forward only, the body
  ends only when every reference is defined and every push popped.

At each step, tokens outside the grammar get probability zero. The grammar accepts 956/956 unseen generator bodies,
so it removes nothing the generators can produce. Bodies cut off by the context are closed (missing labels defined
at the end, open pushes popped). Generation uses a key-value cache, which matches the full forward pass to within 1e-7.

Measured on a 140k-parameter smoke-test model (CPU): 383/383 grammar-constrained samples assemble, against 0/127
without the grammar; 128 bodies in about 4 s. The fine-tuning job samples the real models both ways (the grammar
ablation).
