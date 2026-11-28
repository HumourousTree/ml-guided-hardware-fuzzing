#!/usr/bin/env python3
"""Per-bug comparison of campaign profiles on the published Encarsia bugs (replay-confirmed detections only).

  python tools/profile_compare.py --core rocket NAME=DIR:SUFFIX [NAME=DIR:SUFFIX ...] [--json out.json]

DIR holds <kind>_<dir>_seed<s><SUFFIX>.json and confirmations.json (a merged campaign artifact or logs/bench/<core>).
Prints per bug the seeds that detect it per profile, the bugs found per profile, bugs found by one profile only, and the
union with the published fuzzers (docs/encarsia_published.json).
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(d: Path, sfx: str, bugs) -> dict:
    conf = json.loads((d / "confirmations.json").read_text()) if (d / "confirmations.json").exists() else {}
    out = {}
    for k, b in bugs:
        seeds = []
        for s in range(10):
            f = d / f"{k}_{b}_seed{s}{sfx}.json"
            if not f.exists():
                continue
            r = json.loads(f.read_text())
            fd = r.get("first_detection")
            if fd and conf.get(f.name, {}).get("confirmed", True):
                seeds.append(fd["n_programs"])
            else:
                seeds.append(None)
        out[(k, b)] = seeds
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default="rocket")
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--json")
    a = ap.parse_args()
    pub = json.loads((ROOT / "docs" / "encarsia_published.json").read_text())[a.core]
    bugs = [("driver", d) for d in sorted(pub["driver_dirs"])] + [("multiplexer", d) for d in sorted(pub["multiplexer_dirs"])]
    published = {}
    for k, key, dirs in (("driver", "mixups", "driver_dirs"), ("multiplexer", "conditionals", "multiplexer_dirs")):
        for fz, nums in pub["detected"][key].items():       # published bug numbers: 1-based positions in the dir list
            for i in nums:
                published.setdefault((k, sorted(pub[dirs])[i - 1]), set()).add(fz.split("_")[0])
    res = {}
    for spec in a.runs:
        name, rest = spec.split("=", 1)
        d, sfx = rest.rsplit(":", 1)
        res[name] = load(Path(d), sfx, bugs)
    names = list(res)
    print(f"{'bug':<18}" + "".join(f"{n:>12}" for n in names) + "  published")
    found = {n: set() for n in names}
    for kb in bugs:
        row = []
        for n in names:
            s = res[n][kb]
            det = sum(x is not None for x in s)
            if det:
                found[n].add(kb)
            row.append(f"{det}/{len(s)}" if s else "-")
        if any(kb in found[n] for n in names):
            print(f"{'M' if kb[0] == 'driver' else 'C'}{sorted(pub[kb[0] + '_dirs']).index(kb[1]) + 1:<3}{kb[0][:3]}/{kb[1]:<9}" + "".join(f"{x:>12}" for x in row) + "  " + ",".join(sorted(published.get(kb, []))))
    print("bugs found: " + ", ".join(f"{n} {len(found[n])}" for n in names))
    for n in names:
        only = found[n] - set().union(*(found[m] for m in names if m != n))
        beyond = {kb for kb in found[n] if not published.get(kb)}
        print(f"  {n}: only this profile {sorted(only)}; missed by all published fuzzers {sorted(beyond)}")
    if a.json:
        Path(a.json).write_text(json.dumps({n: {f"{k}/{b}": v for (k, b), v in res[n].items()} for n in names}, indent=1))


if __name__ == "__main__":
    main()
