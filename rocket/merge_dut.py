#!/usr/bin/env python3
"""Merge Cascade's Rocket receptor with one Encarsia host netlist into a single DUT source.

  python3 rocket/merge_dut.py <cascade_receptor.v> <host.v> <dut.v>

The host netlist (Yosys export of the host RTLIL) defines `Rocket` *and* its submodules (ALU, CSRFile, MulDiv, IBuf, ...),
and the receptor still contains the original copies of those submodules.  With two definitions Verilator keeps only one
(MODDUP), which could silently drop a bug injected into a submodule.  Every receptor module that the host netlist also
defines is therefore removed, so the host's version is the only one.  The list of removed modules is printed.
"""
import re
import sys
from pathlib import Path

MOD = re.compile(r"^\s*module\s+(\\?\S+?)\s*[(#;]", re.M)


def modules(text: str) -> set[str]:
    return {m.group(1) for m in MOD.finditer(text)}


def strip(text: str, names: set[str]) -> tuple[str, list[str]]:
    out, removed, i = [], [], 0
    for m in MOD.finditer(text):
        if m.group(1) not in names:
            continue
        start = text.rfind("\n", 0, m.start()) + 1
        end = re.compile(r"^\s*endmodule\b.*$", re.M).search(text, m.end())
        if not end:
            raise SystemExit(f"no endmodule for {m.group(1)}")
        out.append(text[i:start]); i = end.end(); removed.append(m.group(1))
    out.append(text[i:])
    return "".join(out), removed


def main():
    receptor, host, dut = (Path(p) for p in sys.argv[1:4])
    h = host.read_text()
    r, removed = strip(receptor.read_text(), modules(h))
    left = modules(r) & modules(h)
    if left:
        raise SystemExit(f"still duplicated: {sorted(left)}")
    dut.write_text(r + "\n" + h)
    print("replaced from host netlist:", ", ".join(sorted(removed)))


if __name__ == "__main__":
    main()
