# Checkpointed oracle (#4) on Rocket: a negative result with a mechanism

**Setup.** This is a paired comparison on Rocket base, 5 seeds × 30 bugs, -O1 simulators, filter quirks:

| | Run | Commit | Oracle |
|---|---|---|---|
| Checkpointed | 46125031794 | 0edbfc6 | `BENCHFUZZ_CHECKPOINT=1`: x1..x30 dumped after every generator block |
| Baseline | 46122923685 | 05d3617 | end state only |

Both runs use the same random stream, so the program bodies are identical and only the inserted dumps differ. The comparison script is `tools/cov_compare.py`; its output is in `rocket_ck1_compare.txt`.

## Calibration

All 6 bug-free reference simulators gave 0 mismatches on the checkpointed programs, so the checkpoints raise no false alarms. This repeats the local test of 240 programs at K=4 and K=1.

## Detection

| | End state (baseline) | Checkpoint K=1 |
|---|---|---|
| Bugs detected | 10 | 9 |
| Seeds with a detection | 50 | 45 |
| Detection earlier / later / same (45 shared seeds) | – | 0 / 1 / 44 |
| Seconds per program | 0.129 | 0.406 (3.1x) |

- No bug was found only with checkpoints.
- No seed detected earlier.
- Bug driver 673, which the end-state oracle finds on 5 of 5 seeds (after 1–9 programs), is never found with checkpoints: 0 mismatches in 10,000 programs.

## Why driver 673 is lost: the probe effect

The first program that detects it (seed 2) contains this sequence in a single generator block:

```
divuw x11, x18, x1
sub   x11, x0, x6
```

Here a long-latency divide is followed directly by a write of the same register, and the end-state oracle sees x11 wrong.

Two independent checks confirm that this pair is the trigger:
- **Minimisation:** `results/v0.9/minimize/rocket.json` reduces the bug's detections to exactly such a pair, 1-minimal, in two seeds:
  - `divuw x11, x18, x1; sub x11, x0, x6`
  - `divuw x4, x7, x30; sraiw x4, x1, 29`
- **Location:** the bug's netlist location (`data/mutants/published_locations.json`) is Rocket's instruction buffer (`IBuf`, signals `io_inst_0_bits_inst_rd` and `exp_io_out_rd`). This is consistent with the trigger, though the mechanism is inferred rather than traced in the waveform: the second instruction waits there while the divide is busy, and the mix-up corrupts its destination field.

With checkpoints, a dump falls between the two instructions:

```
divuw x11, ...
<dump x1..x30>      # includes sd x11
sub   x11, ...
```

The dump's `sd x11` has to read x11, so the store is now the instruction that waits behind the divide. Presumably, then, the corruption hits a store, which has no destination register, and by the time `sub x11` issues the divide has finished, so the corrupted field is never used.

## Conclusion

The inserted checkpoints are instructions, so they change the timing behaviour they are meant to observe. A per-retirement check that does not perturb the core has to observe passively, for example through a commit trace or trace port, hashed on an FPGA. It cannot be built from code inserted into the program.

The mode stays available behind `BENCHFUZZ_CHECKPOINT` and is off by default.
