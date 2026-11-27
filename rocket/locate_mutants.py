#!/usr/bin/env python3
"""Where each mutant is (Track 1, bug-targeted generation): module and signals an Encarsia-style bug changes.

  python3 rocket/locate_mutants.py --core rocket --kind driver --dir ~/ext/mutants/rocket/driver [--manifest M.json]
                                   --out locations.json

Unlike rocket/locate_bugs.py (majority vote across the corpus) this diffs each mutant against its own bug-free
reference netlist, so it also works on one mutant:
  driver       host_driver.rtlil vs reference_driver.rtlil in the same directory; a signal mix-up rewires one
               `connect` (or a cell port), so the changed lines name the signals involved.
  multiplexer  host_amt.rtlil vs <dir>/../reference.rtlil (or the corpus reference); the bug is the $amt cell whose
               body differs, and its ports name the condition / data signals.
Output: {dir: {"modules": [...], "signals": [...], "lines": n}} (signals are wire names without the leading backslash,
at most 12, most frequent first). Read by rocket/genmodel/data.py to add a location control token.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

MOD = re.compile(r"^module (\S+)\n(.*?)^end\n", re.M | re.S)
WIRE = re.compile(r"\\([A-Za-z_][\w$.\[\]]*)")


def modules(txt: str) -> dict[str, str]:
    return {m.group(1).lstrip("\\"): m.group(2) for m in MOD.finditer(txt)}


def signals(lines: list[str]) -> list[str]:
    c = collections.Counter()
    for l in lines:
        for w in WIRE.findall(l):
            w = re.sub(r"\[\d+(:\d+)?\]$", "", w).lstrip("_")
            if w and w not in ("src", "keep", "hdlname", "init", "STATE_TABLE", "A", "S", "Y", "B"):
                c[w] += 1
    return [w for w, _ in c.most_common(12)]


def context(body: str, changed: list[str]) -> list[str]:
    """Lines around the change that name real signals: for a rewired driver, every other line using the exposed wire
    (its readers); for a changed $amt truth table, the connections of that cell."""
    out = []
    for l in changed:
        m = re.match(r"\s*connect (\S+) ", l)
        if m and "expose_cells" in m.group(1):
            w = m.group(1)
            out += [x for x in body.splitlines() if w in x and x not in changed]
        elif "STATE_TABLE" in l:
            i = body.find(l)
            if i >= 0:
                j = body.find("\n  end", i)
                out += [x for x in body[i:j].splitlines() if x.strip().startswith("connect")]
    return out


def diff_lines(a: str, b: str) -> list[str]:
    """Lines of b not in a and of a not in b (multiset), attribute lines dropped."""
    ca, cb = collections.Counter(a.splitlines()), collections.Counter(b.splitlines())
    out = list((cb - ca).elements()) + list((ca - cb).elements())
    return [l for l in out if not l.strip().startswith("attribute")]


def locate(host: Path, ref: Path) -> dict:
    h, r = modules(host.read_text(errors="replace")), modules(ref.read_text(errors="replace"))
    mods, lines = [], []
    for name, body in h.items():
        if name in r and r[name] != body:
            d = diff_lines(r[name], body)
            if d:
                mods.append(name); lines += d + context(body, d)
    return {"modules": mods, "signals": signals(lines), "lines": len(lines)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True); ap.add_argument("--kind", choices=["driver", "multiplexer"], required=True)
    ap.add_argument("--dir", type=Path, required=True, help="directory holding one subdirectory per mutant")
    ap.add_argument("--manifest", type=Path, help="only the mutants in this manifest (default: every subdirectory)")
    ap.add_argument("--reference", type=Path, help="multiplexer reference.rtlil (default: <dir>/reference.rtlil, "
                                                   "then ~/ext/encarsia/out/<core>/multiplexer/reference.rtlil)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    dirs = ([m["dir"] for m in json.loads(a.manifest.read_text())["mutants"]] if a.manifest
            else sorted(int(p.name) for p in a.dir.iterdir() if p.name.isdigit()))
    ref_mux = a.reference or next((p for p in (a.dir / "reference.rtlil", a.dir.parent / "reference.rtlil",
                                               Path.home() / "ext/encarsia/out" / a.core / "multiplexer/reference.rtlil")
                                   if p.exists()), None)
    res = {}
    for d in dirs:
        p = a.dir / str(d)
        if a.kind == "driver":
            res[str(d)] = locate(p / "host_driver.rtlil", p / "reference_driver.rtlil")
        else:
            res[str(d)] = locate(p / "host_amt.rtlil", ref_mux)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"core": a.core, "kind": a.kind, "locations": res}, indent=1))
    found = sum(1 for v in res.values() if v["modules"])
    print(f"{a.core} {a.kind}: located {found}/{len(res)}; modules "
          f"{collections.Counter(m for v in res.values() for m in v['modules']).most_common(8)}")


if __name__ == "__main__":
    main()
