#!/usr/bin/env python3
"""Which module holds each EnCorpus bug of Rocket or BOOM (for the analysis table; not used by the fuzzer).

  python3 rocket/locate_bugs.py [rocket|boom]  -> logs/bench/<core>/bug_modules.json  {"driver": {dir: module}, ...}

Signal mix-ups (driver): the module whose text differs from the version most other bug netlists have.
Broken conditionals (multiplexer): every multiplexer of the design is an $amt cell in every host netlist; the injected bug
is the cell whose truth table differs from the one most other bug netlists have for that cell.
"""
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = sys.argv[1] if len(sys.argv) > 1 else "rocket"
CORPUS = Path.home() / "ext" / "encarsia" / "out" / CORE


def modules(txt: str) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in re.finditer(r"^module (\S+)\n.*?^end\n", txt, re.M | re.S)}


def amt_cells(txt: str) -> dict[tuple[str, str], str]:
    out, mod = {}, None
    for m in re.finditer(r"^module (\S+)|^\s*cell \$amt (\S+)\n(.*?)^\s*end", txt, re.M | re.S):
        if m.group(1):
            mod = m.group(1)
        else:
            out[(mod, m.group(2))] = m.group(3)
    return out


def main():
    dirs = lambda k: sorted(int(p.name) for p in (CORPUS / k).iterdir() if p.name.isdigit())
    res = {"driver": {}, "multiplexer": {}}
    hosts = {d: modules((CORPUS / "driver" / str(d) / "host_driver.rtlil").read_text()) for d in dirs("driver")}
    for d, host in hosts.items():
        diff = sorted(m.lstrip("\\") for m, t in host.items()
                      if collections.Counter(hosts[e].get(m) for e in hosts if e != d).most_common(1)[0][0] != t)
        res["driver"][d] = ", ".join(diff) or "?"
    cells = {d: amt_cells((CORPUS / "multiplexer" / str(d) / "host_amt.rtlil").read_text()) for d in dirs("multiplexer")}
    for d, cs in cells.items():
        diff = set()
        for key, body in cs.items():
            common = collections.Counter(cells[e].get(key) for e in cells if e != d).most_common(1)[0][0]
            if common != body:
                diff.add(key[0].lstrip("\\"))
        res["multiplexer"][d] = ", ".join(sorted(diff)) or "?"
    out = ROOT / "logs" / "bench" / CORE / "bug_modules.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res))


if __name__ == "__main__":
    main()
