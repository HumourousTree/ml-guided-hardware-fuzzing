# Track 2: dual-hart PicoRV32 with a replayable arbiter log (design)

Goal: extend BenchFuzz's exact per-retirement oracle, minimisation and certified repair to a two-core system with shared
memory, and make concurrency failures **replayable** (and therefore minimisable), which the leading multi-hart fuzzer
(HartBreaker, ISCA 2026) states it cannot do.

## Why this is tractable

There are no caches and one bus. Every memory access of both harts passes through a single round-robin arbiter, so the
system is sequentially consistent and the arbiter's **grant log is a total order of all data accesses**. A reference
model that executes each hart independently but performs memory accesses in grant-log order reproduces the run exactly,
so both RVFI streams can be checked in lockstep as for one core. Recording the grant log of a failing run, and forcing
the same grant order on replay, makes the failure deterministic while the program is minimised.

Limit: no reordering is possible in this system, so weak-memory bugs (HartBreaker's class) are out of scope. Bus
contention, arbitration, atomicity and handshake bugs are in scope.

## RTL (`hw_design/dual/`)

* `dual_top.v`: two `picorv32` instances (the pristine upstream RTL, `hw_design/upstream_picorv32.v`, unchanged; `ENABLE_PCPI=1`), each with its own program region
  (`PROGADDR_RESET` 0x0000 / 0x4000) and a shared data region (0x8000-0x8FFF), plus one AMO unit per hart.
* `arbiter_rr.v`: round-robin arbiter over four requesters (hart 0/1 native bus, AMO unit 0/1) with a **lock** input
  so an AMO's read and write are one indivisible pair; a grant-log port (`log_valid`, `log_id`, `log_instr`,
  `log_addr`, `log_wstrb`, `log_data`, one pulse per completed transaction) and a replay input (`force_valid`,
  `force_id`): while it is set, the next **data** transaction (load, store, AMO) can only go to `force_id`.
  Instruction fetches stay free, since their order has no architectural effect; this is what keeps replay valid
  while a program is being shrunk.
* `amo_pcpi.v`: executes `amoswap.w` arriving on PicoRV32's co-processor interface (PCPI): lock the bus, read the old
  word, write rs2, return the old word as rd. The core itself is not modified.

## Testbench and oracle

* `hw_design/dual/dual_tb.cpp`: services the single arbitrated memory port (seeded random wait states), prints both
  RVFI streams with a hart id and the grant log as JSON lines; an optional replay file (requester ids of the data
  grants of an earlier run) drives `force_id`. Entries of a hart that has halted are skipped.
* Reference (`harness/dual_cosim.py`): two instances of the Python RV32IM model over one shared memory, stepped so that
  loads, stores and AMOs happen in grant-log order (fetches come from private program regions and need no ordering).
  `amoswap.w` is executed by the wrapper (PicoRV32 reports a PCPI instruction with `rd_wdata` only).
* Verdict: each hart's RVFI stream must equal its model stream (single-core rules: same fields, no missing or extra
  retirements; the trap on the zero word after the program is the normal end). The log is checked against the ISA as
  well: every data grant must be the hart's next memory access (requester, address, strobes), an AMO is a read grant
  immediately followed by its own write grant (atomicity), and a hart still holding an access when the run times out
  is reported as starved.

## Stimulus

Paired programs: private ALU/memory work, shared-region loads/stores, `amoswap.w`-based locks (spinlocks with bounded
retries, so a correct system always terminates), flag/handshake patterns, contention bursts.

## Bug class to inject (new tier L4: concurrency)

Arbiter starvation (round-robin pointer not advanced), lost write under contention (grant switched mid-transaction),
non-atomic AMO (lock released between read and write), wrong grant id in the log path (an oracle-visible fault),
handshake (`mem_ready`) routed to the wrong hart.

## Evaluation plan

1. Soundness: zero mismatches on the bug-free system across seeds and memory delays (E0 analogue).
2. Detection of every injected L4 bug; time to bug.
3. Minimisation with forced grant order: failing pairs shrink and still fail; compare with minimisation without
   replay (failures vanish), which is the HartBreaker limitation.
4. Certified repair of L4 bugs (gates + Yosys equivalence of the whole system).

## Status (v0.9)

Built and running: RTL (lint-clean with `-Wall`), testbench, reference, generator (`harness/dual_gen.py`: private
work, shared loads/stores, AMOs, spinlock critical sections, flag handshakes, same-word bursts; every loop bounded),
campaigns (`python -m harness.dual_campaign soundness | bugs | minimise`, workflow `dual-hart.yml`). Results:
`results/v0.9/dual/` (GitHub Actions run 45791198894, identical to the local runs).

* **Soundness:** 500 random pairs x 4 memory timings = 2,000 runs, **0 mismatches**. In 492 of the 500 pairs the
  order of data accesses changes with the memory timing, so the oracle sees many interleavings.
* **Replay:** forcing a recorded data order reproduces a run exactly (same order, same traces) under a different
  memory timing.
* **L4 bugs** (`hw_design/dual/l4_bugs.json`, ten one-line changes to the arbiter or AMO unit): **8/10 detected**,
  each within the first two pairs. Not detected: `starve_pointer` (fixed priority is a legal schedule and every loop
  is bounded: a pure liveness/performance bug) and `lost_write_same_word`, which is **unreachable** here: it needs
  both harts to have a data request pending at a grant, but after any transaction a hart's next request is a fetch,
  and the only other requesters are the AMO units of stalled harts (directed test: 0 triggers). It is the
  dual-hart analogue of an equivalent mutant.
* **Reproduction under a new timing:** a detected failure re-run under 16 other memory timings reproduces 16/16 with
  the recorded data order; without it, 14/16 for the narrow races (`amo_race_window`, `stale_rdata_amo`; 7/16 before
  the generator's same-word burst block raised contention) and 15/16 for `ready_misroute`.
* **Minimisation** (ddmin over the source lines of both harts, 300 tests per bug and mode; a candidate counts only
  if it fails on the buggy system with the original kind *and* passes on the bug-free one):

  | bug | lines | free: left / tests | forced data order: left / tests |
  |---|---|---|---|
  | amo_not_atomic | 289 | 3 / 99 | 2 / 102 |
  | lost_write_contention | 289 | 1 / 80 | 5 / 118 |
  | log_id_amo | 289 | 2 / 43 | 8 / 126 |
  | stale_rdata | 289 | 8 / 67 | 9 / 69 |
  | lock_stuck | 289 | 2 / 25 | 2 / 25 |
  | ready_misroute | 187 | 13 / 300 (budget) | **2 / 175** |
  | amo_race_window | 187 | 13 / 300 (budget) | **5 / 235** |
  | stale_rdata_amo | 187 | 19 / 300 (budget) | **13 / 292** |

  Every minimised witness passes on the bug-free system and reproduces under 16/16 other timings. Reading: the
  simulator is deterministic for a fixed timing, so plain ddmin already works for bugs that fire whenever the code
  runs; for the narrow races it stalls at the budget, because removing lines shifts the timing out of the race
  window, while the forced data order keeps the window open and reaches 2-5 lines in fewer tests. The recorded order
  is followed until it no longer fits the shrunk program (no data grant for 3,000 cycles), then arbitration is free.
* **Oracle corrections found by the clean-system check** (first cloud minimisation): a log cut off by the cycle limit
  between an AMO's read and write was reported as non-atomic, and a hart still making progress at the limit as
  starved; both are now "program does not terminate" (shrinking had removed loop counters). The reference also caps
  how far it models a hart, which had exhausted the runner's memory on such loops.

Limits: one shared bus and no caches, so no weak-memory behaviour; two harts; only `amoswap.w` (no LR/SC).
