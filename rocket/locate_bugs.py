#!/usr/bin/env python3
"""Which Rocket module holds each EnCorpus bug (for the analysis table; not used by the fuzzer).

  python3 rocket/locate_bugs.py      -> logs/bench/rocket/bug_modules.json  {"driver": {dir: module}, "multiplexer": {...}}

Signal mix-ups (driver): the module whose text differs from the version most other bug netlists have.
Broken conditionals (multiplexer): every multiplexer of the design is an $amt cell in every host netlist; the injected bug
is the cell whose truth table differs from the one most other bug netlists have for that cell.
"""
import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path.home() / "ext" / "encarsia" / "out" / "rocket"


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
    out = ROOT / "logs" / "bench" / "rocket" / "bug_modules.json"
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res))


if __name__ == "__main__":
    main()
