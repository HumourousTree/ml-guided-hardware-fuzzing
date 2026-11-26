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
  so an AMO's read and write are one indivisible pair; outputs `grant_valid`, `grant_id`, `grant_addr`, `grant_we`
  for the log, and a `force_id` input used on replay.
* `amo_pcpi.v`: executes `amoswap.w` arriving on PicoRV32's co-processor interface (PCPI): lock the bus, read the old
  word, write rs2, return the old word as rd. The core itself is not modified.

## Testbench and oracle

* C++ testbench (extends `harness/riscv_cosim.py`'s embedded one): services the single arbitrated memory port, prints
  both RVFI streams with a hart id and the grant log; `--replay <log>` drives `force_id` from a recorded log.
* Reference: two instances of the Python RV32IM model sharing one memory, stepped so that loads, stores and AMOs happen
  in grant-log order (instruction fetches come from private program regions and need no ordering). AMO effects come
  from the log, since they do not appear in the core's RVFI memory fields.
* Verdict: each hart's RVFI stream must equal its model stream (same rules as today: no missing or extra retirements),
  and the final shared memory must agree.

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

## Status

Design (this file). Next: arbiter + AMO unit RTL with unit tests, then the testbench and two-hart reference model.
