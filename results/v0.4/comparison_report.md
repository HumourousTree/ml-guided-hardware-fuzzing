# BenchFuzz comparative analysis (generated)


## E0 control
- YosysHQ master (ef203c2): 150/150 runs matched, 1599 retired instructions, aligned convention ok=yes, Cascade-P6 FENCE probe matches, FENCE-rd probe x2=0
- f00a88c (Cascade) (f00a88c): 150/150 runs matched, 1599 retired instructions, aligned convention ok=yes, Cascade-P6 FENCE probe trap, FENCE-rd probe trap
- golden copy (--): 150/150 runs matched, 1599 retired instructions, aligned convention ok=yes, Cascade-P6 FENCE probe matches, FENCE-rd probe x2=0

## E1 bug suite
- 14/15 bugs detected by their targeted stimulus; localisation contains the fault for 14/14 (primary block 9/14)

- E2 experiment_e2_v2trio.json: 3/3 concurrently injected bugs isolated

- E2 experiment_e2_v2five.json: 5/5 concurrently injected bugs isolated

## Coverage benchmark (Verilator line+toggle+branch points, PicoRV32)
- Random: 63.79±0.00% at N=10000 programs (n=7); plateau at 133 programs (7/7 runs); 186 insn/s per process
- Cov.-guided mutation: 64.57±0.08% at N=10000 programs (n=7); plateau at 227 programs (7/7 runs); 371 insn/s per process
- Hybrid (mut.->rand.): 64.21±0.06% at N=10000 programs (n=7); plateau at 227 programs (7/7 runs); 132 insn/s per process
- Mann-Whitney p: random vs mutation: 0.000603, random vs hybrid-rand: 0.000768, mutation vs hybrid-rand: 0.00128

## Time-to-bug (blind fuzzing, budget 1,000 programs, 10 seeds)
- alu_operator_xor_or [L1]: random 10/10 (median 7.0 progs), mutation 10/10 (median 14.5 progs)
- and_to_or_alu [L1]: random 10/10 (median 2.0 progs), mutation 10/10 (median 2.0 progs)
- approx_multiplier [L1]: random 2/10 (median 554.0 progs), mutation 0/10 (median None progs)
- fast_mul_decode_and_to_or [L1]: random 0/10 (median None progs), mutation 0/10 (median None progs)
- fast_mul_rs2_signed_widened [L1]: random 10/10 (median 10.5 progs), mutation 10/10 (median 52.0 progs)
- mem_addr_bit2_stuck [L1]: random 10/10 (median 1.0 progs), mutation 10/10 (median 1.0 progs)
- slt_signed_as_unsigned [L1]: random 10/10 (median 1.0 progs), mutation 10/10 (median 1.5 progs)
- beq_inverted [L2]: random 10/10 (median 3.0 progs), mutation 10/10 (median 2.5 progs)
- branch_inversion_blt [L2]: random 10/10 (median 3.0 progs), mutation 10/10 (median 8.5 progs)
- lui_auipc_rd_x0 [L2]: random 10/10 (median 1.0 progs), mutation 10/10 (median 1.0 progs)
- writeback_forwarding_drop [L2]: random 10/10 (median 1.0 progs), mutation 10/10 (median 1.0 progs)
- mem_handshake_drop [L3]: random 10/10 (median 4.0 progs), mutation 10/10 (median 3.5 progs)
- stale_latched_branch [L3]: random 10/10 (median 1.0 progs), mutation 10/10 (median 1.0 progs)
- store_after_store_dropped [L3]: random 10/10 (median 2.0 progs), mutation 10/10 (median 2.0 progs)
- store_after_store_dropped_intermittent [L3]: random 10/10 (median 11.0 progs), mutation 10/10 (median 21.0 progs)

## Cascade P6 (FENCE) reproduction
- Cov.-guided mutation on f00a88c: 10/10 seeds hit P6, median 4 programs, 0.74 s
- Random on f00a88c: 10/10 seeds hit P6, median 2 programs, 0.15 s
- control on master (Cov.-guided mutation): 1 mismatches in 1249 programs
- control on master (Random): 0 mismatches in 2000 programs
- master, Cov.-guided mutation: 9/10 seeds hit a mismatch (9 at a FENCE with rd!=0), median 872 programs
- master, Random: 0/10 seeds hit a mismatch (0 at a FENCE with rd!=0), median -- programs

## Delta debugging on fuzzer-found failures (80 cases, 14 bugs)
- median length 36 -> 3 instructions (93.1% reduction), cycles reduced 86.2% (median)
- tests: ddmin median 6, HDD median 7; HDD fewer tests in 29/80; 1-minimal: ddmin 100%, HDD 100%

- mem_handshake_drop template patch: only ['mem_la_write (top-level output, look-ahead memory interface)'] not proven equal (2400 points proven)

## Guarded repair (no LLM key available: controls + template baseline)
- oracle accepted 14/14, lint-broken patch recovered on retry 14/14, no-op rejected 14/14
- template repair: accepted 9/14, correct on held-out 9, exact 4, overfitting accepted 0; rejections by gate: {'patch_format': 0, 'lint': 0, 'reproducer': 269, 'grow_prefix': 0, 'suite_gate': 5}

## Unified coverage
- hybrid-rand seed 0: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 64.15, 'C_ISS': 89.83, 'C_func': 97.46, 'C_U': 79.12}
- hybrid-rand seed 1: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 64.06, 'C_ISS': 89.83, 'C_func': 99.13, 'C_U': 79.51}
- hybrid-rand seed 2: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 64.32, 'C_ISS': 89.83, 'C_func': 97.46, 'C_U': 79.16}
- mutation seed 0: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 64.64, 'C_ISS': 89.83, 'C_func': 97.46, 'C_U': 79.24}
- mutation seed 1: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 64.76, 'C_ISS': 89.83, 'C_func': 95.8, 'C_U': 78.86}
- mutation seed 2: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 64.69, 'C_ISS': 89.83, 'C_func': 95.8, 'C_U': 78.84}
- random seed 0: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 63.84, 'C_ISS': 89.83, 'C_func': 97.46, 'C_U': 79.04}
- random seed 1: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 63.55, 'C_ISS': 89.83, 'C_func': 99.13, 'C_U': 79.38}
- random seed 2: {'n_programs': 3000, 'C_line': 65.03, 'C_toggle': 63.84, 'C_ISS': 89.83, 'C_func': 97.46, 'C_U': 79.04}
- bins never hit by any run: fence, jalr
- benchmark instruction mix (%): {"ALU-imm": 45.4, "ALU-reg": 11.5, "MUL/DIV": 5.4, "Branch": 6.9, "Jump": 1.5, "Load": 4.9, "Store": 4.4, "LUI/AUIPC": 20.0}

## Unreached RTL after saturation (random, all seeds)
- Counter CSRs (outside RV32IM scope): 2 regions, 8 lines
- Compressed ISA (COMPRESSED_ISA=0): 28 regions, 40 lines
- ALU options (BARREL_SHIFTER, TWO_CYCLE_*): 3 regions, 5 lines
- ld_rs2 state (unused, ENABLE_REGS_DUALPORT=1): 4 regions, 6 lines
- External PCPI (ENABLE_PCPI=0): 1 regions, 1 lines
- Interrupts (ENABLE_IRQ=0): 14 regions, 14 lines
- Trap on illegal instruction (excluded by validator): 1 regions, 1 lines

## Archived 552-iteration campaign (Run C, Gemini)
- instruction mix (%): {"MUL_DIV": 2.7, "ALU_IMM": 57.5, "LUI_AUIPC": 2.2, "BRANCH": 8.2, "ALU_REG": 17.3, "STORE": 5.5, "LOAD": 4.5, "JUMP": 2.0}
- Gemini expansion: 133 attempts, 5 accepted, 126 rejected

## Yosys synth_xilinx (xcup) for PicoRV32 in the harness configuration
- 1502 LUTs, 945 FFs, 4 DSP48E2 (0.29% of XCZU19EG LUTs; pre place-and-route estimate)

## Literature (quoted, not re-measured)
- ChipFuzzer: Wang et al., When Fuzzing Meets Understanding: LLM-Driven Semantic Test Generation for RTL Verification, arXiv:2607.10340v1, 11 Jul 2026 (peer-reviewed: False); comparable: method only (plateau rule); numbers no
- Cascade: Solt, Ceesay-Seitz, Razavi, Cascade: CPU Fuzzing via Intricate Program Generation, USENIX Security 2024 (peer-reviewed: True); comparable: P6 only: same DUT commit f00a88c, reproduced by BenchFuzz (E0 P6 probe and blind fuzzing)
- TheHuzz: Kande et al., TheHuzz: Instruction Fuzzing of Processors Using Golden-Reference Models for Finding Software-Exploitable Vulnerabilities, USENIX Security 2022 (peer-reviewed: True); comparable: no
- TurboFuzz: TurboFuzz: FPGA Accelerated Hardware Fuzzing for Processor Agile Verification, arXiv:2509.10400v2, 3 Feb 2026 (peer-reviewed: False); comparable: throughput order of magnitude only (different DUT and harness)
- BMCFuzz: BMCFuzz: Hybrid Verification of Processors by Synergistic Integration of Bound Model Checking and Fuzzing (IEEE, 2025) (peer-reviewed: True); comparable: no
- Encarsia: Bolcskei, Solt, Ceesay-Seitz, Razavi, Encarsia: Evaluating CPU Fuzzers via Automatic Bug Injection, USENIX Security 2025 (peer-reviewed: True); comparable: no (different designs); our 15-bug PicoRV32 suite follows the same 'one bug per design copy, detection = ISA-visible divergence' protocol
