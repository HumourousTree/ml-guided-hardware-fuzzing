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
| Stimulus | `rocket/rv64fuzz.py`: random RV64IMFD + Zicsr programs, no coverage feedback | DifuzzRTL / ProcessorFuzz (coverage on and off), Cascade |
| Reference | Spike (riscv-isa-sim, pinned commit) | Spike (DifuzzRTL, ProcessorFuzz); Cascade checks its own programs |
| Oracle | end state: x1-x30, f0-f31, fcsr, mscratch and 32 data dwords dumped through Cascade's MMIO port, compared with Spike; non-termination is also a detection | per fuzzer |
| Budget | 5 seeds x up to 2,000 programs per bug (minutes per bug) | 24 h per bug on one core |

BenchFuzz's per-retirement (lockstep) oracle is not used here: the Rocket netlists have no RVFI port. On PicoRV32 the
end-state oracle detects the same bugs as lockstep, only later (study B1, `docs/EXPERIMENTS.md`).

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

A one-bit change of the expected values is detected by the comparison (sanity check of the oracle).

## Reproduce

```bash
bash scripts/setup_rocket_env.sh
python3 rocket/run_encarsia.py --seeds 5 --programs 2000 --jobs 6 --build-jobs 2    # -> logs/bench/rocket/
python tools/compare_literature.py                                                  # table tab_rocket, figure fig_encarsia_rocket
```

The campaign is resumable: each bug and seed writes its own result file as soon as it finishes, and finished files are
skipped on a rerun (only runs in progress at the interruption start over). After a closed session, a reboot or a WSL
restart, `bash scripts/resume_background.sh` restarts whatever is unfinished (Rocket campaign and the LLM runner, see
`docs/EXPERIMENTS.md`), and `bash scripts/resume_background.sh status` shows what is running. From Windows, detached:
`Start-Process wsl -WindowStyle Hidden -ArgumentList '-d Ubuntu -- bash "<repo in /mnt/c>/scripts/resume_background.sh"'`.
