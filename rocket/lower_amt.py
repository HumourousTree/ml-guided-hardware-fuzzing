#!/usr/bin/env python3
"""Lower Encarsia's `$amt` cells to standard `$eq` + `$pmux` cells so stock Yosys can read the netlist.

  python3 rocket/lower_amt.py <in.rtlil> <out.rtlil>

Encarsia's broken-conditional bugs (EnCorpus `multiplexer/`) are RTLIL netlists containing `$amt` cells, an internal cell of
the encarsia-yosys fork.  This reimplements its `inject_map` pass (encarsia-yosys 24b5066, passes/inject/inject_map.cc and
selection.h) as a text transform:
  STATE_TABLE = N entries of (S-width select pattern over {0,1,-}, 1 "buggy" flag bit), entry i at bit offset i*(|S|+1),
  pattern bit j compares S[j]; entry i selects A[i*|Y| +: |Y|].
  Y = $pmux(A = 0, B = A, S = {eq_i}), eq_i = (S bits where the pattern is 0/1) == (those pattern bits); an entry whose
  pattern is all '-' always selects (the same as an $eq of zero width in Yosys, which is 1).
"""
import re
import sys
from pathlib import Path

WIRE = re.compile(r"^\s*wire\s+(.*?)(\\\S+|\$\S+)\s*$")
TOKEN = re.compile(r"\{|\}|\d+'[01xzm-]*|-?\d+|[\\$]\S+(?:\s*\[\d+(?::\d+)?\])?")


def sig_width(sig: str, widths: dict[str, int]) -> int:
    """Width of an RTLIL sigspec (constants, wires, wire[i], wire[m:n], { ... } concatenations)."""
    total = 0
    for t in TOKEN.findall(sig):
        if t in "{}":
            continue
        if "'" in t:
            total += int(t.split("'")[0])
        elif re.fullmatch(r"-?\d+", t):
            total += 32
        else:
            m = re.match(r"([\\$]\S+?)\s*\[(\d+)(?::(\d+))?\]$", t)
            if m:
                total += 1 if m.group(3) is None else abs(int(m.group(2)) - int(m.group(3))) + 1
            else:
                total += widths[t]
    return total


def lower_module(lines: list[str], uid: list[int]) -> tuple[list[str], int]:
    widths = {}
    for ln in lines:
        m = WIRE.match(ln)
        if m:
            w = re.search(r"width\s+(\d+)", m.group(1))
            widths[m.group(2)] = int(w.group(1)) if w else 1
    out, i, n = [], 0, 0
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^\s*cell\s+\$amt\s", ln):
            ind = re.match(r"^(\s*)", ln).group(1)
            j = i + 1
            params, conns = {}, {}
            while not re.match(r"^\s*end\s*$", lines[j]):
                p = re.match(r"^\s*parameter\s+\\(\S+)\s+(.*)$", lines[j])
                c = re.match(r"^\s*connect\s+\\(\S+)\s+(.*)$", lines[j])
                if p:
                    params[p.group(1)] = p.group(2).strip()
                if c:
                    conns[c.group(1)] = c.group(2).strip()
                j += 1
            ws, wy = sig_width(conns["S"], widths), sig_width(conns["Y"], widths)
            table = params["STATE_TABLE"].split("'", 1)[1]
            bits = table[::-1]                                         # bits[k] = STATE_TABLE bit k (LSB first)
            entries = len(bits) // (ws + 1)
            assert entries * (ws + 1) == len(bits), "STATE_TABLE size"
            assert sig_width(conns["A"], widths) == entries * wy, "A width"
            k = uid[0]; uid[0] += 1
            s_w, a_w, sel_w = f"\\amt{k}_s", f"\\amt{k}_a", f"\\amt{k}_sel"
            out += [f"{ind}wire width {ws} {s_w}", f"{ind}wire width {entries * wy} {a_w}", f"{ind}wire width {entries} {sel_w}",
                    f"{ind}connect {s_w} {conns['S']}", f"{ind}connect {a_w} {conns['A']}"]
            for e in range(entries):
                pat = bits[e * (ws + 1): e * (ws + 1) + ws]            # pat[j] compares S[j]
                care = [jj for jj, b in enumerate(pat) if b in "01"]
                if not care:
                    out.append(f"{ind}connect {sel_w} [{e}] 1'1"); continue
                a_sig = "{ " + " ".join(f"{s_w} [{jj}]" for jj in reversed(care)) + " }"
                b_sig = f"{len(care)}'" + "".join(pat[jj] for jj in reversed(care))
                out += [f"{ind}cell $eq $amt{k}_eq{e}", f"{ind}  parameter \\A_SIGNED 0", f"{ind}  parameter \\B_SIGNED 0",
                        f"{ind}  parameter \\A_WIDTH {len(care)}", f"{ind}  parameter \\B_WIDTH {len(care)}",
                        f"{ind}  parameter \\Y_WIDTH 1", f"{ind}  connect \\A {a_sig}", f"{ind}  connect \\B {b_sig}",
                        f"{ind}  connect \\Y {sel_w} [{e}]", f"{ind}end"]
            out += [f"{ind}cell $pmux $amt{k}_pmux", f"{ind}  parameter \\WIDTH {wy}", f"{ind}  parameter \\S_WIDTH {entries}",
                    f"{ind}  connect \\A {wy}'" + "0" * wy, f"{ind}  connect \\B {a_w}", f"{ind}  connect \\S {sel_w}",
                    f"{ind}  connect \\Y {conns['Y']}", f"{ind}end"]
            n += 1; i = j + 1
            continue
        out.append(ln); i += 1
    return out, n


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    lines = src.read_text().splitlines()
    out, mod, total, uid = [], None, 0, [0]
    for ln in lines:
        if re.match(r"^\s*module\s", ln):
            mod = [ln]; continue
        if mod is not None:
            mod.append(ln)
            if re.match(r"^\s*end\s*$", ln) and not any(re.match(r"^\s*(cell|process|switch|case|sync|memory)\b", x)
                                                         for x in [mod[-2]] if False):
                # a module ends at a top-level 'end' (indentation 0)
                if ln.startswith("end"):
                    body, n = lower_module(mod, uid); out += body; total += n; mod = None
            continue
        out.append(ln)
    dst.write_text("\n".join(out) + "\n")
    print(f"lowered {total} $amt cells")


if __name__ == "__main__":
    main()
