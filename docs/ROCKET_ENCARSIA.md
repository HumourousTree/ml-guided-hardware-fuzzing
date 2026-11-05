# Step 3: BenchFuzz on Rocket against Encarsia's published fuzzer results

## Why Rocket (and not CVA6 or Ibex)

Encarsia (USENIX Security 2025) publishes per-bug detection results only for **Rocket and BOOM** (Tables 8-10:
DifuzzRTL, ProcessorFuzz, Cascade). Its corpus also contains Ibex bugs, but only with formal proofs and no fuzzer results,
and CVA6 is not part of the fuzzer evaluation. Rocket is the smallest core with a published baseline, so it is the core on
which BenchFuzz can be compared with other fuzzers on *the same bugs*. The published per-bug results are in
`docs/encarsia_published.json`, with the source and the directory-to-bug-number mapping.

## What is compared

| | BenchFuzz (this repo) | Published (Encarsia) |
|---|---|---|
| DUT | Encarsia's Rocket SoC (Cascade "tiny SoC", `cascade_receptor.v`) with one EnCorpus host netlist | same netlists |
| Stimulus | `rocket/rv64fuzz.py`, no coverage feedback. Profile **base**: random RV64IMFD + Zicsr on fcsr/frm/fflags/mscratch, no exceptions. Profile **ext**: base + machine-mode CSRs, exceptions and compressed instructions (below) | DifuzzRTL / ProcessorFuzz (coverage on and off), Cascade |
| Reference | Spike (riscv-isa-sim, pinned commit) | Spike (DifuzzRTL, ProcessorFuzz); Cascade checks its own programs |
| Oracle | end state: x1-x30, f0-f31, fcsr, mscratch and 32 data dwords (ext: also mstatus, mepc, mcause, mtval, mie) dumped through Cascade's MMIO port, compared with Spike; non-termination is also a detection | per fuzzer |
| Budget | base: 5 seeds x up to 2,000 programs per bug; ext: 5 seeds x up to 400 (minutes per bug) | 24 h per bug on one core |

BenchFuzz's per-retirement (lockstep) oracle is not used here: the Rocket netlists have no RVFI port. On PicoRV32 the
end-state oracle detects the same bugs as lockstep, only later (study B1, `docs/EXPERIMENTS.md`).

## The ext profile, and why it exists

The base campaign missed three broken conditionals that published fuzzers find. `rocket/locate_bugs.py` shows that 11 of
the 15 broken conditionals (including all three) are in Rocket's `CSRFile`, which the base profile reaches only through the
FP CSRs and mscratch, and that most undetected signal mix-ups touch `RVCExpander`, the compressed-instruction decoder, while
the base profile emits no compressed code. The ext profile adds exactly these, so it was designed *after* seeing the base
misses: its results are diagnostic, not a blind second trial.

* machine-mode CSR traffic: set/clear of mstatus FS and MPIE bits, reads of mstatus/mepc/mcause/mtval, read-write of mie;
* exceptions: ecall, ebreak, illegal encodings, misaligned loads and stores, FP instructions with an invalid frm or with the
  FPU switched off. A trap handler folds mcause, mepc and (for address faults) mtval into x29 and resumes after the
  faulting instruction (x28 is its temporary; neither is a body destination);
* compressed instructions: the assembler may compress everything except the trapping instructions (always 4 bytes, since
  the handler skips 4), plus explicit `c.*` ALU operations and `c.beqz`/`c.bnez` forward branches (about 20% of the
  executed instructions are compressed).

Comparing against Spike without false alarms required neutralising behaviour the ISA leaves to the implementation. Each was
found by calibration on the golden references and is commented in the code:

| Difference between Rocket and Spike | Handling |
|---|---|
| reset value of mstatus.MPP (Rocket M, Spike U) | the prologue clears MPP |
| mtval after ebreak / illegal instruction (Rocket 0, Spike pc / instruction bits; both allowed) | handler folds mtval only for address faults, then clears it |
| WARL width of mepc / mtval / mcause writes (Rocket keeps ~40 address bits) | those CSRs are only read in the body |
| mstatus.FS becomes Dirty conservatively on Rocket | FS and SD are masked out of body reads of mstatus (the epilogue sets FS = Dirty on both) |
| Spike's timer interrupt is always pending (the tiny SoC has none) | mstatus.MIE is never set |

`--priv=m` for Spike is *not* a fix: Rocket implements S and U (mstatus.UXL/SXL = 2). With these rules both golden
references give 0 mismatches on 500 calibration programs each, and base-profile programs are byte-identical to before
(`tests/test_rv64fuzz.py`).

Example: mix-up 7 (dir 767, `RVCExpander`), found by no published fuzzer. The buggy core takes an exception on the legal
compressed branch `c.bnez a1` (Spike and the golden Rocket do not), visible in the dump as a non-zero trap accumulator x29.

## Pipeline (all user space, inside WSL)

1. `bash scripts/setup_rocket_env.sh`: RISC-V binutils and dtc (Ubuntu packages extracted with `dpkg -x`), Spike built
   from source (commit `1b9f9c8`, 2026-10-21; v1.1.0 does not compile with GCC 15), the EnCorpus (Encarsia `b8fd17d`),
   encarsia-meta (`756ba14`) and Cascade's Rocket testbench (cascade-chipyard `727a99a`, sparse checkout). Cascade's
   common testbench files come from Encarsia's Cascade fork (encarsia-artifacts/encarsia-cascade `2e90bec`).
2. `rocket/lower_amt.py` (broken conditionals only): Encarsia's netlists contain `$amt` cells, an internal cell of its
   Yosys fork. This file reimplements its `inject_map` pass (`$amt` -> `$eq` + `$pmux`) so stock Yosys 0.52 can read them.
3. `rocket/build_dut.sh <rtlil> <dir>`: Yosys export of the host module; `rocket/merge_dut.py` removes from the receptor
   every module the host netlist defines (ALU, CSRFile, MulDiv, IBuf, ...), so the host's possibly buggy version is the only
   one (with duplicates Verilator keeps one silently); Verilator 5.032 build with Cascade's testbench.
4. `rocket/run_encarsia.py`: builds all 30 bugs and both golden references, **calibrates** each golden reference against
   Spike (must give 0 mismatches before any bug is fuzzed), then fuzzes every bug.

## Problems met and how they were handled

| Problem | Fix |
|---|---|
| `comsec-group/cascade-meta` does not exist; git hung on a credential prompt | use Encarsia's fork `encarsia-artifacts/encarsia-cascade`, the version its results were produced with |
| Spike v1.1.0 does not compile with GCC 15; the shallow clone lacked the softfloat submodule | pinned newer commit, `git submodule update --init` |
| `$amt` cells unreadable by stock Yosys | `rocket/lower_amt.py`; validated: the lowered multiplexer reference matches Spike on 500 programs |
| receptor and host both define Rocket's submodules | `rocket/merge_dut.py` keeps the host's version only |
| Cascade's `sram_mem.sv` declares a 100M-entry load buffer: Verilator needed > 8 GB, exhausted the WSL VM and restarted it (killing every other job) | patched copy `rocket/sram_mem.sv` with a 1 MiB buffer; builds limited to one compile job, `-O0`, split output, per-process memory cap. A build now takes ~50 s and 3.5 GB |
| Spike and the Rocket boot ROM leave different values in a0/a1 | every program initialises all registers before use |

## Calibration (both must hold before fuzzing)

| Reference | Programs | Mismatches | Non-terminating |
|---|---|---|---|
| driver reference (bug 767's `reference_driver.rtlil`) | 500 | 0 | 0 |
| multiplexer reference, `$amt` lowered | 500 | 0 | 0 |

A one-bit change of the expected values is detected by the comparison (sanity check of the oracle). The ext profile is
calibrated separately (`golden_<kind>_ext.json`): 500 programs per reference, 0 mismatches.

## Replay of every detection (`rocket/confirm_detections.py`)

Calibration is a sample. After the campaigns, the first detecting program of every run is replayed on the bug-free Rocket and
on the buggy one; a detection counts only if the bug-free Rocket matches Spike and the buggy one does not.

| | detections | confirmed | bug-free Rocket also differs from Spike | did not reproduce |
|---|---|---|---|---|
| base + ext | 150 | 133 | 16 (all ext) | 1 (base, mix-up 13 seed 0) |

All 16 reference disagreements come from ext programs that switch the FPU off (mstatus.FS = Off): Spike then traps FP
instructions and FP-CSR accesses that the bug-free Rocket executes in some cases. 500 calibration programs did not reveal
this. Whether it is a Rocket deviation was not investigated. These runs count as not detected (they stopped at the false
alarm, so the true bug was not searched for further in that seed). A future ext revision should avoid FS = Off.

## Results (2026-11-05)

Replay-confirmed detecting seeds out of 5 (median programs to the detection). Published: Encarsia Tables 8-10, 24 h.
Mod. = module of the injected broken conditional (`rocket/locate_bugs.py`).

| Bug | base (2,000 progs/seed) | ext (400 progs/seed) | DifuzzRTL | ProcessorFuzz | Cascade |
|---|---|---|---|---|---|
| Mix-up 1 | 5/5 (1) | 5/5 (1) | | | x |
| Mix-up 2-4 | 5/5 (1) | 5/5 (1) | x | x | x |
| Mix-up 5 | 0/5 | 5/5 (1) | | | |
| Mix-up 6 | 5/5 (2) | 5/5 (11) | | | x |
| Mix-up 7 | 0/5 | 5/5 (2) | | | |
| Mix-up 8 | 0/5 | 5/5 (3) | | | |
| Mix-up 9 | 0/5 | 4/5 (46) | | | |
| Mix-up 10 | 0/5 | 5/5 (1) | | | |
| Mix-up 11 | 0/5 | 0/5 | | | |
| Mix-up 12 | 0/5 | 5/5 (1) | | | |
| Mix-up 13 | 4/5 (2) | 5/5 (1) | x | x | x |
| Mix-up 14 | 0/5 | 0/5 | | | |
| Mix-up 15 | 5/5 (1) | 5/5 (1) | x | x | x |
| Cond. 1-5 (CSR) | 0/5 | 0/5 | | | Cond. 2 |
| Cond. 6 (CSR) | 0/5 | 5/5 (1) | x | x | x |
| Cond. 7 (CSR) | 5/5 (1) | 5/5 (2) | x | x | |
| Cond. 8-11 (CSR) | 0/5 | 0/5 | Cond. 10 | Cond. 10 | |
| Cond. 12 (IBuf) | 5/5 (1) | 5/5 (1) | x | x | x |
| Cond. 13 (RVC), 14 (core) | 0/5 | 0/5 | | | |
| Cond. 15 (core) | 5/5 (1) | 5/5 (1) | x | x | x |

* Signal mix-ups: base 7/15 (exactly Cascade's set), ext 13/15; **6 mix-ups (5, 7, 8, 9, 10, 12) are detected by none of
  the published fuzzers**. Only 11 and 14 are found by nobody.
* Broken conditionals: base 3/15, ext 4/15 (6, 7, 12, 15), against 5 (DifuzzRTL, ProcessorFuzz) and 4 (Cascade). Missed:
  2 (Cascade) and 10 (DifuzzRTL/ProcessorFuzz), both in `CSRFile`. 7 of the 11 `CSRFile` conditionals are found by no fuzzer.
* Detection is all-or-nothing per bug and fast (median 1 program); budgets are minutes per bug against 24 h.
* Example (mix-up 7, `RVCExpander`): the buggy core takes an exception on the legal compressed branch `c.bnez`.

Paper: Sec. "Same bugs, same core", Table `tab:rocket`, Fig. `fig:rocket` (`paper/figures/fig_encarsia_rocket.pdf`).
Packaged results: `results/v0.4/bench/rocket/` (runs with the detecting program, calibrations, `confirmations.json`,
`bug_modules.json`).

## Reproduce

```bash
bash scripts/setup_rocket_env.sh
python3 rocket/run_encarsia.py --seeds 5 --programs 2000 --jobs 6 --build-jobs 2    # base profile -> logs/bench/rocket/
python3 rocket/run_encarsia.py --profile ext --seeds 5 --programs 400 --jobs 6      # ext profile (*_ext.json)
python3 rocket/locate_bugs.py                                                       # module of each bug
python3 rocket/confirm_detections.py 6                                              # replay every detection
python tools/compare_literature.py                                                  # table tab_rocket, figure fig_encarsia_rocket
```

The campaign is resumable: each bug and seed writes its own result file as soon as it finishes, and finished files are
skipped on a rerun (only runs in progress at the interruption start over). After a closed session, a reboot or a WSL
restart, `bash scripts/resume_background.sh` restarts whatever is unfinished (Rocket campaign and the LLM runner, see
`docs/EXPERIMENTS.md`), and `bash scripts/resume_background.sh status` shows what is running. From Windows, detached:
`Start-Process wsl -WindowStyle Hidden -ArgumentList '-d Ubuntu -- bash "<repo in /mnt/c>/scripts/resume_background.sh"'`.
