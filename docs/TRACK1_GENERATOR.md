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
| 5. Pretrain | `rocket/genmodel/{data,train}.py` | Kaggle (`rocket/genmodel/kaggle/`) | done: small (run 1), base (run 2) |
| 6. SFT + DPO on kills | `rocket/genmodel/train.py sft / dpo` | Kaggle (`rocket/genmodel/kaggle_ft/`) | done: small, base; on-policy round 2 |
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
  dump and trap handler as every other profile, with the same per-core quirk filters (enforced since 2026-11-28, see
  "Two harness bugs" below; BOOM results before that are not used).

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
unfused kernel. Fixed (fp16 + loss scaling below compute capability 8).

**Pretraining, run 2** (`base`, 85M, fp16, 5000 steps of 32 x 1024 tokens, 1.7 s per step, 2.4 h): validation loss
1.921 (step 500) -> 1.876 (3000) -> 1.864 (5000), against 1.869 for `small`. Eight times the parameters buy 0.005
nats per token: the pretraining corpus is at its entropy floor, so `small` is the efficient choice for this stage, and
`base` is kept only to see whether capacity helps once the kill signal is added. Grammar-constrained samples: 100%
pass the harness check.

**Fine-tuning, run 1** (`small`, Kaggle T4, 1 min of training in all). Data: 720 gated pool programs (Rocket and
BOOM, 120 per core x profile), weighted by the training mutants they kill (477 training mutants have a killer), and
1481 (killer, non-killer) pairs. SFT, 150 steps: train loss 1.88 -> 1.69, validation 2.146 -> 2.143. DPO, 200 steps:
loss 0.69 -> 0.61. SFT shortens bodies (base 43 -> 34 lines); DPO brings them back (46).
Grammar ablation, same DPO model: with the grammar 4608/4608 samples (pre, SFT, DPO x 2 cores x 3 profiles) assemble;
without it 706/762 (92.6%; spliced operands, duplicate labels, out-of-range constants, a write to `x30`).
Kill rates on dev mutants and on the published Encarsia bugs: `genmodel-eval.yml` (pool of 120 gate-passing samples
per stage x profile, the same size as a generator profile).

**Evaluation on Rocket** (`results/v0.9/genmodel/eval/`, `tools/genmodel_stats.py`). Pools of 120 gate-passing
samples per stage x profile (360 programs, the generators' pool size); 119 dev mutants (60 driver, 59 conditional)
and the 30 published Encarsia Rocket bugs. Intervals: 2000 random pools of 60 programs per profile, against an
independent generator pool of the same size.

| Stage | dev killed (of 119) | diff vs generators, 60/profile (95%) | published killed (of 30) |
|---|---|---|---|
| generators | 44 | | 16 |
| small pre / SFT / DPO | 45 / 45 / 45 | +1.6 [-1, +4] / +0.7 [-2, +3] / +1.0 [-2, +3] | 16 / 16 / 16 |
| base pre / SFT / DPO | 44 / 45 / 45 | +0.9 [-1, +3] / +1.2 [-1, +3] / +1.0 [-1, +3] | 17 / 16 / 16 |
| DPO without grammar (small / base) | 43 / 44 | -0.1 [-2, +2] / +0.3 [-2, +2] | 16 / 17 |
| round 2: on-policy + rarity (small_op1) | 45 | +1.5 [-1, +4] | 17 |
| round 2: rarity only (small_rar) | 44 | +0.5 [-2, +3] | 16 |
| round 3: generator kills, after the fixes (small_r3g) | 46 | +2.1 [+0, +4] | 16 |
| round 3: + on-policy kills (small_r3) | 44 | +1.4 [+0, +3] | 17 |

Round 3 (runs 46036238505 Rocket, 46036243632 BOOM) is round 2 retrained after the two harness fixes below, with DPO
batch 32. Without the on-policy data (small_r3g) it is the best model so far on both cores, +2.1 and +2.0 mutants,
but still not significant: in 8.7% (Rocket) and 17.2% (BOOM) of the 2000 subsample pairs it kills no more than the
generators. Its gains are single mutants killed by a few samples (Rocket driver/139, /554, /896; BOOM driver/28,
/236, /253, /751), against one generator-only mutant per core. Adding the model's own kills (small_r3) is worse on
both cores, so the on-policy labels (2 new Rocket kills, 4 BOOM) are too few to help. Published bugs: unchanged,
except one small_r3 sample killing Rocket mix-up 9 again (`driver/804`).

Round 2 (run 46023954014) trained before the two harness fixes below. The on-policy model has the best pool-size curve
so far (about 1.3 mutants ahead of the generators at 40 programs per profile); rarity weighting alone adds nothing, so
the small gain comes from the model's own kills. No interval excludes 0.

The learned generator matches the generators; every interval includes 0, so there is no significant gain, and
fine-tuning on kills changes little (kills per program and mutant: generators 0.212, small DPO 0.214). The one
difference on published bugs is Encarsia's Rocket mix-up 9 (`driver/804`), which none of DifuzzRTL, ProcessorFuzz or
Cascade detects: two `base` samples kill it (pre and DPO-without-grammar, ext profile, register mismatch; re-run
46032665516 reproduces both) and none of the 360 pool programs does. It is not beyond our generators: the Step 1
campaigns find it with ext (5/5 seeds, median 62 programs) and the LLM templates (3/5); it is only rare in a
360-program pool.

**Evaluation on BOOM** (round 1, quirk-fixed samples, run 46032275438). Same design: 120 dev mutants (60 driver, 60
conditional) and the 30 published Encarsia BOOM bugs, quirk filter on for model and generators alike.

| Stage | dev killed (of 120) | diff vs generators, 60/profile (95%) | published killed (of 30) |
|---|---|---|---|
| generators | 46 | | 18 |
| small DPO | 47 | +0.9 [-2, +4] | 18 |
| base DPO | 46 | -0.1 [-3, +3] | 19 |
| round 3: generator kills, after the fixes (small_r3g) | 48 | +2.0 [-1, +5] | 18 |
| round 3: + on-policy kills (small_r3) | 46 | +0.6 [-2, +4] | 18 |

BOOM agrees with Rocket: no interval excludes 0 (kills per program and mutant: generators 0.170, small DPO 0.171).
The extra published bug of base DPO is conditional C4 (`multiplexer/160`, `BranchKillableQueue_5`), killed by one
sample of the LLM-template profile; it is not beyond our generators, since the Step 1 LLM-template campaign finds it on
5/5 seeds.

**On-policy round 1.** 720 samples per core from small DPO (half at temperature 1.0, half at 1.3), labelled against a
fixed random 100 per core x kind of the train mutants that no generator program kills (`genmodel-onpolicy.yml`).
Rocket: 2 such mutants killed (4 kills in 48,000 runs). BOOM: 25 (588 kills), but 21 of them needed `fsqrt`/`fdiv.d`,
which the generators are barred from on BOOM: the first harness bug below. Temperature 1.3 gave about twice the
new kills of 1.0. The model learned `fsqrt` from Rocket's LLM templates (130/300 programs) and used it on BOOM, so the
core token is weak; BOOM's divide/square-root unit is untested by every generator here (and by Cascade, which filters
it too).

**Two harness bugs (found 2026-11-28, both fixed).**
1. `gen_program(body=...)` did not apply the core quirk filters, so on BOOM 19% of the small model's samples used
   barred instructions (`fdiv.d`, `fsqrt`). Bodies now get the quirks of the profile they stand for; generator
   programs are byte-identical (1,800 checked). All BOOM samples were re-wrapped (same seeds) and BOOM is evaluated
   again under equal rules; the earlier BOOM runs were cancelled and are not used. Rocket is unaffected (no quirks).
2. `data.body_from_asm` took the last `fsrmi x0,` as the end of the prologue; ~20% of generator-pool bodies contain
   their own, so their SFT/DPO training bodies lost their front part. Fixed (the prologue has exactly one); round trip
   exact on all 7,627 Rocket samples. Fine-tuning runs 1-3 trained on the truncated bodies; later rounds do not.

**Upstream reports.** The two core issues found by calibration are already known upstream: BOOM executing FP
instructions with `dyn` rounding while `frm` is invalid is riscv-boom#752 (open; also #492, found before by DifuzzRTL
and MorFuzz), and the PicoRV32 FENCE `rd` write is picorv32#293 (closed; the repository is archived). Nothing new
to file. The injector bug was filed as encarsia-yosys#1.

**Bug-targeted generation (round 4, 2026-11-28).** Idea: tell the model where the bug is, so it writes programs for
that part of the core.
* Locations: `rocket/locate_mutants.py` diffs each mutant against its own bug-free reference netlist (driver: the
  rewired `connect` and the lines that read the exposed wire; conditional: the `$amt` cell whose truth table changed
  and its ports). Published bugs: 60/60 located (`data/mutants/published_locations.json`). Factory mutants
  (`locate-mutants.yml`, run 46055843112): the regenerated netlists match all 1,595 manifest hashes, 1,577 located.
  41 location classes (module name without the Chisel instance suffix, >= 5 train mutants), `rocket/genmodel/modules.json`.
* Model: one `ctl:mod:<class>` control token per class (plus `other`), appended after the existing tokens; a
  pretrained checkpoint's embedding is grown (`GPT.grow_vocab`: old logits unchanged, max diff 0.0). Targeted data
  (`data.py sft --targeted`): each killer also appears with the token of the classes of the train mutants it kills,
  weighted by those kills only; DPO pairs carry the mutant's token. Round 4 (`small_tgt`, Kaggle, 25 min): 733 killer
  programs -> 10,783 sequences, 1,494 pairs; SFT 800 steps (val 1.91), DPO 300 (loss 0.51, preference accuracy 0.75).
* Result: **the token is barely used.** Conditioning on the right class changes the log-likelihood of a whole killer
  body by +-0.05 nats (SFT; DPO: -0.15 to -0.36, the same on random bodies), and the samples' instruction mix is the
  same for every class (e.g. compressed instructions 15-17% for every Rocket class incl. RVCExpander; mul/div 8.4-9.3%
  for MulDiv and the rest). Classifier-free guidance (`generate(guidance=g)`, cond + g(cond - uncond)) does not recover
  it: no consistent shift at g = 2, 4, 8, and bodies get shorter (44 -> 34 lines at g = 8)
  (`tools/genmodel_token_diag.py`, `tools/genmodel_cfg_test.py`, `results/v0.9/genmodel/r4/`).
* Why: the signal in the data is weak. Killers of a class's mutants differ from the pool only in frequency
  (RVCExpander killers 19.7% compressed vs 12.9%; FDivSqrtUnit killers 2.2% `fdiv`/`fsqrt` vs 0.3%; FP-unit killers
  14-27% FP vs 12%), and the same programs kill mutants of many classes (easy mutants are killed by most of the pool).
  Keeping only the killers of hard mutants (<= 25% or 10% of the pool) does not sharpen it. Programs sampled from
  random generators are not location-specific, so a model trained on their kills cannot learn location from them.
  A location-aware generator needs location-specific training programs (programs that cover the module, or that are written for it).
* Kill-rate test (Rocket; `results/v0.9/genmodel/r4/eval/`, `tools/genmodel_targeted.py`). 64 samples per module
  class x profile with the token, each labelled only against the mutants of its class (`genmodel-eval.yml
  match_class=true`, run 46063024035), against 120 per profile from the same model without it (run 46063027788).
  Paired per (mutant, profile), over the cells either side kills:

  | Split | per-program kill rate, targeted vs untargeted | diff (95%, bootstrap over mutants) | higher / lower | mutants killed, 54 programs/profile |
  |---|---|---|---|---|
  | dev (118 mutants) | 0.702 vs 0.689 | +0.014 [+0.003, +0.027] | 45 / 29 (sign p = 0.08) | 43.9 vs 42.4 |
  | published (30) | 0.734 vs 0.740 | -0.006 [-0.024, +0.012] | 14 / 15 | 16.8 vs 16.4 (17 vs 17 all) |

  A small effect on dev, almost all from MulDiv (+0.045 over 40 cells; the others are within +-0.025), and none on
  the published bugs. Two rare dev mutants of the `Rocket` class are killed only with the token (driver/906, /951, 1-2
  programs), one MulDiv mutant only without (driver/112, 1 program). The untargeted model matches the generators
  (43 vs 44 dev mutants, +0.0 [-2, +2]). The token is a weak steer for an arithmetic unit and nothing more, as the
  likelihood test predicted; no BOOM run (it would cost ~35 Actions jobs for an effect this size).

**Decoding speed.** The key-value cache is preallocated (one position written per step; the first version
concatenated, which copies the cache every token), the grammar mask is built on the host and sent in one copy, the
sampled tokens are read back once per step, and finished sequences leave the batch. On a CPU, 32 kill-conditioned ext
bodies from `small`: 8.7 -> 4.0 s unconstrained, 10.6 -> 4.4 s with the grammar, so the grammar costs ~10%.

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
so it removes nothing the generators can produce (re-checked after every change; latest 774/774). A body cannot be
empty. Bodies cut off by the context are closed (missing labels defined at the end, open pushes popped). Generation
uses a key-value cache; cached and full-forward logits agree to fp32 rounding (both within 3e-4 of an fp64 forward,
logits up to 21), also after finished rows are dropped.

Measured on a 140k-parameter smoke-test model (CPU): 383/383 grammar-constrained samples assemble, against 0/127
without the grammar; 128 bodies in about 4 s. The fine-tuning job samples the real models both ways (the grammar
ablation).
