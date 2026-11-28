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

Here a long-latency divide is followed directly by a write of the same register. The end-state oracle sees x11 wrong, so the bug lets the divide's late writeback overwrite the newer value.

With checkpoints, a dump falls between the two instructions:

```
divuw x11, ...
<dump x1..x30>      # includes sd x11
sub   x11, ...
```

The dump's `sd x11` has to read x11, so the pipeline waits for the divide to finish. The two writes are then no longer in flight together, and the hazard never fires.

## Conclusion

The inserted checkpoints are instructions, so they change the timing behaviour they are meant to observe. A per-retirement check that does not perturb the core has to observe passively, for example through a commit trace or trace port, hashed on an FPGA. It cannot be built from code inserted into the program.

The mode stays available behind `BENCHFUZZ_CHECKPOINT` and is off by default.
