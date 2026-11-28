#!/usr/bin/env python3
"""Rewrite every `$pmux` cell of an RTLIL netlist as a first-match chain of `$mux` cells.

  python3 rocket/pmux_first_match.py <in.rtlil> <out.rtlil>

A `$pmux` with more than one select bit set has no single meaning: the simulators run the casez that `write_verilog`
emits for it (the lowest set bit wins), Yosys' own simulation model gives x, and Yosys SAT (equiv_simple, equiv_induct,
sat) lets the highest set bit win. A mutant can make a select multi-hot that never was in the reference, and a proof
under the SAT reading then says nothing about the simulated core. After this rewrite every tool reads the netlist as
the simulator does: Y = S[0] ? B[0] : S[1] ? B[1] : ... : A.
"""
import re
import sys
from pathlib import Path

PARAM = re.compile(r"^\s*parameter\s+\\(\S+)\s+(.*)$")
CONN = re.compile(r"^\s*connect\s+\\(\S+)\s+(.*)$")


def rewrite(lines):
    out, i, k = [], 0, 0
    while i < len(lines):
        ln = lines[i]
        if not re.match(r"^\s*cell\s+\$pmux\s", ln):
            out.append(ln); i += 1; continue
        ind = re.match(r"^(\s*)", ln).group(1)
        name = ln.split()[2]
        p, c, j = {}, {}, i + 1
        while not re.match(r"^\s*end\s*$", lines[j]):
            m = PARAM.match(lines[j]) or CONN.match(lines[j])
            if m:
                (p if lines[j].lstrip().startswith("parameter") else c)[m.group(1)] = m.group(2).strip()
            j += 1
        # drop attributes that preceded the cell (they belong to it)
        while out and out[-1].lstrip().startswith("attribute"):
            out.pop()
        w, n = int(p["WIDTH"]), int(p["S_WIDTH"])
        b, s = f"\\pfm{k}_b", f"\\pfm{k}_s"
        out += [f"{ind}wire width {w * n} {b}", f"{ind}wire width {n} {s}",
                f"{ind}connect {b} {c['B']}", f"{ind}connect {s} {c['S']}"]
        prev = c["A"]
        for e in reversed(range(n)):
            y = c["Y"] if e == 0 else f"\\pfm{k}_c{e}"
            if e:
                out.append(f"{ind}wire width {w} {y}")
            out += [f"{ind}cell $mux {name}_fm{e}", f"{ind}  parameter \\WIDTH {w}", f"{ind}  connect \\A {prev}",
                    f"{ind}  connect \\B {b} [{(e + 1) * w - 1}:{e * w}]", f"{ind}  connect \\S {s} [{e}]",
                    f"{ind}  connect \\Y {y}", f"{ind}end"]
            prev = y
        k += 1; i = j + 1
    return out, k


def main():
    lines = Path(sys.argv[1]).read_text().splitlines()
    out, k = rewrite(lines)
    Path(sys.argv[2]).write_text("\n".join(out) + "\n")
    print(f"rewrote {k} $pmux cells")


if __name__ == "__main__":
    main()
