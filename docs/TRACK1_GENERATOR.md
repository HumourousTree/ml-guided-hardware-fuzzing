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
| 3. Labels | `rocket/label_mutants.py label` | GitHub Actions (20 shards) | done: Rocket and BOOM, both kinds |
| 4. Tokenizer | `rocket/isa_tokenizer.py` | anywhere | done, lossless |
| 5. Pretrain | `rocket/genmodel/{data,train}.py` | Kaggle (`rocket/genmodel/kaggle/`) | small done; base running |
| 6. SFT + DPO on kills | `rocket/genmodel/train.py sft / dpo` | Kaggle (`rocket/genmodel/kaggle_ft/`) | small running |
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

## Results so far

**Pretraining, run 1** (Kaggle T4, commit ae942f2, 400k bodies = 127M tokens). `small` (11M), 6000 steps of
32 x 1024 tokens: validation loss 6.34 -> 1.94 (step 500) -> 1.876 (step 3000) -> 1.869 (step 6000). The curve is flat
after step ~3000, at the entropy of the generators themselves (immediates and register choices are random), so more
pretraining buys little; what the model learns beyond syntax has to come from the kill signal (SFT/DPO).
Unconstrained samples (128 per core x profile): 100% decode, 99.6% pass the harness check, 90.4% assemble
(683/756; the failures are operands spliced from two instructions, e.g. `sub x10,x8,c.and,x15`), 44 / 50 / 74 lines
for base / ext / LLM-template, matching the generators. The grammar removes the assembly failures by construction.
`base` (85M) ran out of memory in this run: `torch.cuda.is_bf16_supported()` is true on a T4 (it counts emulation),
so training ran in emulated bf16, which is slow (1.9 s per step even for `small`) and sends attention to the
unfused kernel. Fixed (fp16 + loss scaling below compute capability 8); run 2 trains `base` only.

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
