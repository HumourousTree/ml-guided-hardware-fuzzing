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

* `dual_top.v`: two `picorv32` instances (unchanged RTL, `ENABLE_PCPI=1`), each with its own program region
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
campaigns (`python -m harness.dual_campaign soundness | bugs | minimise`, results in `logs/dual/`).

* Soundness: 500 random pairs x 4 memory timings = 2,000 runs, **0 mismatches**; in 492 of the 500 pairs the order
  of data accesses changes with the memory timing, so the oracle is exercised on many interleavings.
* Replay: forcing a recorded data order reproduces the run exactly (same order, same traces) under a different
  memory timing.
* L4 bugs (`hw_design/dual/l4_bugs.json`, ten single-line changes to the arbiter or AMO unit): eight detected within
  the first one to three pairs. Not detected: `starve_pointer` (fixed priority is a legal schedule and every loop is
  bounded, so it is a pure liveness/performance bug), and `lost_write_same_word`, which is **unreachable** in this
  composition: it needs both harts to have a data request pending at a grant, but a hart's next request after any
  transaction is a fetch, and the only other requesters are the AMO units of stalled harts (directed test: 0
  triggers). This is the dual-hart analogue of an equivalent mutant.
* Replay value: for the narrow-window races (`amo_race_window`, `stale_rdata_amo`) a failing pair re-run under a new
  memory timing reproduced in 7/16 runs without replay and 16/16 with the recorded data order (with the burst block
  added to the generator, contention is higher and the free rate rises to 14/16).

Limits: one shared bus and no caches, so no weak-memory behaviour; two harts; only `amoswap.w` (no LR/SC).
