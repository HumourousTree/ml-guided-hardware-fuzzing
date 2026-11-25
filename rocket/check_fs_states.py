#!/usr/bin/env python3
"""Rocket vs Spike: mstatus.FS after clearing its two bits together or one at a time (explains the ext v1 false alarms).

  python3 rocket/check_fs_states.py [golden_multiplexer]      (run from the repository root, inside WSL)

Result on 2026-11-25: Rocket stores FS with two states (any non-zero write becomes Dirty = 3), so clearing the bits one at
a time never reaches Off and FP instructions keep executing, while Spike reaches Off and traps. Clearing both bits at
once gives Off and a trap on both. The privileged spec lets an implementation report Dirty conservatively."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rv64fuzz as f  # noqa: E402

SIM = Path.home() / "rocket_runs" / (sys.argv[1] if len(sys.argv) > 1 else "golden_multiplexer") / "Vtop_tiny_soc"
SEQS = {"clear 0x6000 at once": ["li x28, 0x6000", "csrc mstatus, x28"],
        "clear 0x2000, then 0x4000": ["li x28, 0x2000", "csrc mstatus, x28", "li x28, 0x4000", "csrc mstatus, x28"],
        "clear 0x4000, then 0x2000": ["li x28, 0x4000", "csrc mstatus, x28", "li x28, 0x2000", "csrc mstatus, x28"],
        "clear 0x2000 only (FS=Initial by ISA)": ["li x28, 0x2000", "csrc mstatus, x28"],
        "clear 0x4000 only (FS=Clean by ISA)": ["li x28, 0x4000", "csrc mstatus, x28"]}


def program(seq):
    # after the sequence: read FS (x6), then try one FP instruction (traps only if FS = Off; trap count in x29)
    return "\n".join([
        "  .section .text.init", "  .option norvc", "  .globl _start", "_start:",
        "  li x31, 0x6000", "  csrs mstatus, x31", "  la x28, trap", "  csrw mtvec, x28", "  li x29, 0",
        "  li x5, 0x3ff0000000000000", "  fmv.d.x f1, x5"] + ["  " + s for s in seq] + [
        "  csrr x6, mstatus", "  srli x6, x6, 13", "  andi x6, x6, 3", "  fadd.d f3, f1, f1",
        "  li x31, 0x6000", "  csrs mstatus, x31",
        f"  li x31, {f.DUMP}", "  sd x6, 0(x31)", "  sd x29, 0(x31)",
        f"  li x2, {f.STOP}", "  sd x0, 0(x2)", "  la x3, tohost", "  li x4, 1", "  sd x4, 0(x3)", "1: j 1b",
        "  .align 2", "trap:", "  addi x29, x29, 1", "  csrr x28, mepc", "  addi x28, x28, 4", "  csrw mepc, x28", "  mret",
        "  .section .data", "data: .dword 0",
        "  .section .tohost, \"aw\"", "  .align 6", "  .globl tohost", "tohost: .dword 0", "  .globl fromhost",
        "fromhost: .dword 0"]) + "\n"


w = Path(tempfile.mkdtemp())
print(f"{'sequence':40s} {'Spike FS, traps':18s} Rocket FS, traps")
for name, seq in SEQS.items():
    elf = f.build_elf(program(seq), w)
    exp = f.run_spike(elf)
    got, _ = f.run_rocket(SIM, elf, 100000)
    print(f"{name:40s} FS={exp[0]} traps={exp[1]}      FS={got[0]} traps={got[1]}" + ("   <- differs" if exp != got else ""))
