# Real bug: PicoRV32 FENCE writes its reserved `rd` field

| | |
|---|---|
| Design | YosysHQ PicoRV32, final upstream commit `ef203c2` (repository archived, Sep 2026) |
| Configuration | `ENABLE_FAST_MUL=1 ENABLE_DIV=1 REGS_INIT_ZERO=1`, other parameters default |
| Class | Specification deviation, architecturally visible register write (CWE-1281-like: sequence of processor instructions leads to unexpected behaviour) |
| Found by | BenchFuzz coverage-guided mutation fuzzer (lockstep RVFI oracle) with FENCE enabled, 9/10 seeds, median 872 programs; random generator 0/10 (it only emits canonical FENCE words) |
| Status | Not reported before as far as we found (YosysHQ/picorv32 issues incl. #293, Cascade P1–P6, ReFuzz, web search, Nov 2026) |

## Behaviour
```
addi  x1, x0, 123          # x1 = 123
.word 0x0ff0008f           # fence iorw, iorw with the reserved rd field = x1
addi  x2, x1, 0            # expected x2 = 123; PicoRV32 master gives x2 = 0
```
The RISC-V unprivileged ISA says the unused `rd` and `rs1` fields of FENCE are reserved and that *base implementations shall
ignore these fields* (standard software shall zero them). PicoRV32 instead performs a write-back to `rd`.

## Root cause
Upstream PR #229 (fix for Cascade's P6, "fence raises a trap") added `instr_fence` to the decoder so FENCE no longer traps, but
gave it no execution path. FENCE therefore falls into the generic ALU branch of `cpu_state_ld_rs1`, which sets `latched_store`;
`latched_rd` holds the instruction's `rd` field (`decoded_rd <= mem_rdata_latched[11:7]`) like for every instruction, and
`alu_out` keeps its default `'bx` because no ALU operation is selected. The written value is undefined: Verilator evaluates it as
0; synthesised hardware may produce any value. Issue #293 reported that the reserved fields are *decoded* and was closed because
decoding them "has no side effect"; this is the side effect.

On commit `f00a88c` (the one Cascade evaluated) FENCE traps first (P6), which masks this behaviour.

## Fix (`fix.patch`, one line)
Decode the reserved `rd` of MISC-MEM (opcode `0001111`) as x0, so nothing is written back.

## Files
| File | |
|---|---|
| `fix.patch` | Patch against `hw_design/upstream_picorv32.v` |
| `picorv32_fixed.v` | Upstream with the patch applied |
| `verification.json` | Output of `python3 tools/check_real_bug.py` |

`tools/check_real_bug.py` re-checks everything: a sweep over rd = x1…x31 with varied reserved fields (all must diverge on upstream,
none on the fixed core), the E0 control on the fixed core, blind mutation fuzzing of the fixed core with the settings that found
the bug, and Verilator lint. The lockstep probe is also part of E0 (`fence_rd_probe` in `experiment_e0_*.json`).

## Use as a benchmark
A fuzzer finds this bug if it executes a FENCE whose bits 11:7 are non-zero and an oracle compares the destination register
(per-retirement or end-state). It needs no CSR, privilege or interrupt support, so it fits RV32I-only fuzzers, and unlike injected
bugs it is a real defect in a shipped core.
