#!/usr/bin/env python3
"""Spike vs bug-free Rocket vs bug-free BOOM: FP instructions with a dynamic rounding mode while frm is invalid (5-7).

  python3 rocket/check_frm.py      (repository root, inside WSL; needs the golden_multiplexer simulators of both cores)

Result on 2026-11-26: Spike and Rocket raise an illegal-instruction exception, as the ISA requires; the BOOM version
used by Encarsia/Cascade executes the instruction. Explains the 4/1,000 ext calibration failures on BOOM."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rv64fuzz as f  # noqa: E402


def program(frm, insn):
    return "\n".join([
        "  .section .text.init", "  .option norvc", "  .globl _start", "_start:",
        "  li x31, 0x6000", "  csrs mstatus, x31", "  la x28, trap", "  csrw mtvec, x28", "  li x29, 0", "  li x7, 0x1234",
        "  li x5, 0x3ff0000000000000", "  fmv.d.x f1, x5", "  fmv.d.x f3, x0", f"  fsrmi x0, {frm}", f"  {insn}",
        "  fsrmi x0, 0", "  fmv.x.d x6, f3",
        f"  li x31, {f.DUMP}", "  sd x29, 0(x31)", "  sd x6, 0(x31)", "  sd x7, 0(x31)",
        f"  li x2, {f.STOP}", "  sd x0, 0(x2)", "  la x3, tohost", "  li x4, 1", "  sd x4, 0(x3)", "1: j 1b",
        "  .align 2", "trap:", "  addi x29, x29, 1", "  csrr x28, mepc", "  addi x28, x28, 4", "  csrw mepc, x28", "  mret",
        "  .section .data", "data: .dword 0", "  .section .tohost, \"aw\"", "  .align 6", "  .globl tohost",
        "tohost: .dword 0", "  .globl fromhost", "fromhost: .dword 0"]) + "\n"
w = Path(tempfile.mkdtemp())
print(f"{'frm':4s} {'instruction':28s} Spike traps / Rocket traps / BOOM traps")
for frm in (0, 4, 5, 6, 7):
    for insn in ("fadd.d f3, f1, f1, dyn", "fmul.d f3, f1, f1, dyn", "fcvt.d.lu f3, x7, dyn", "fadd.d f3, f1, f1, rne"):
        elf = f.build_elf(program(frm, insn), w); exp = f.run_spike(elf)
        roc, _ = f.run_rocket(Path.home()/"rocket_runs/golden_multiplexer/Vtop_tiny_soc", elf, 100000)
        boo, _ = f.run_rocket(Path.home()/"boom_runs/golden_multiplexer/Vtop_tiny_soc", elf, 100000)
        flag = "   <- BOOM differs" if boo != exp else ""
        print(f"{frm:<4d} {insn:28s} {exp[0]} / {roc[0]} / {boo[0]}{flag}")
