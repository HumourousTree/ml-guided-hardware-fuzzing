#!/usr/bin/env python3
"""Apply the small in-place fixes that are not shipped as whole files.  Idempotent; asserts on mismatch.

  1. hw_design/picorv32_golden.sv + picorv32_mutable.sv: restore upstream `mem_la_addr` (word aligned).
     The prototype had changed it to `reg_op1`, i.e. the DUT was edited to match the ISS convention
     instead of the scoreboard applying the DUT's (riscv-formal ALIGNED_MEM) convention.
  2. dashboard.py: iteration table used legacy telemetry keys (always 0%); crash guard for
     delta-debug panel when no mismatch record exists.
Run from the project root:  python3 tools/apply_fixes.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RTL_OLD = "assign mem_la_addr = (mem_do_prefetch || mem_do_rinst) ? {next_pc[31:2] + mem_la_firstword_xfer, 2'b00} : reg_op1;"
RTL_NEW = "assign mem_la_addr = (mem_do_prefetch || mem_do_rinst) ? {next_pc[31:2] + mem_la_firstword_xfer, 2'b00} : {reg_op1[31:2], 2'b00};"

DASH = [
    ('"RTL Coverage": f"{it.get(\'rtl_coverage_pct\', 0.0)}%",\n            "ISS Coverage": f"{it.get(\'iss_coverage_pct\', 0.0)}%",',
     '"Unified Coverage": f"{it.get(\'unified_coverage_pct\', 0.0)}%",\n            "Functional Coverage": f"{it.get(\'functional_coverage_pct\', 0.0)}%",'),
    ('failing_disasm = delta_debug.get("failing_instruction_disasm") or first_mismatch.get("insn_disasm", "Unknown")',
     'failing_disasm = delta_debug.get("failing_instruction_disasm") or (first_mismatch or {}).get("insn_disasm", "Unknown")'),
    ('failing_hex = delta_debug.get("failing_instruction_hex") or first_mismatch.get("insn_hex", "")',
     'failing_hex = delta_debug.get("failing_instruction_hex") or (first_mismatch or {}).get("insn_hex", "")'),
]


def patch(path: Path, pairs: list[tuple[str, str]]) -> None:
    if not path.exists():
        print(f"[skip] {path} not found"); return
    text = path.read_text()
    for old, new in pairs:
        if new in text:
            print(f"[ok]   {path.name}: already patched"); continue
        assert old in text, f"{path.name}: expected text not found:\n{old}"
        text = text.replace(old, new, 1)
        print(f"[done] {path.name}: patched")
    path.write_text(text)


if __name__ == "__main__":
    for rtl in ("hw_design/picorv32_golden.sv", "hw_design/picorv32_mutable.sv"):
        patch(ROOT / rtl, [(RTL_OLD, RTL_NEW)])
    patch(ROOT / "dashboard.py", DASH)
    print("Done. Now run: python3 -m unittest discover -s tests -t .")
