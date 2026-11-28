"""Per-module mutant kills of two template profiles (label-mutants artifacts), split by train/dev.

  python tools/template_scripts/target_kills.py --labels DIR [DIR ...] [--a llmtgt] [--b llmctl] [--json out.json]

Each DIR is a labels-<core>-<kind> artifact (labels.jsonl) or a stored <core>_<kind>_labels.jsonl.gz. A mutant counts as killed by a profile if any of that
profile's programs kills it. Modules come from data/mutants/<core>_<kind>_locations.json; "targeted" marks the modules
named in rocket/templates/llm_target.json (chosen from training-split unkilled mutants, so dev is held out).
"""
import argparse
import collections
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", nargs="+", required=True)
    ap.add_argument("--a", default="llmtgt")
    ap.add_argument("--b", default="llmctl")
    ap.add_argument("--json")
    a = ap.parse_args()
    targets = json.loads((ROOT / "rocket" / "templates" / "llm_target.json").read_text())["targets"]
    out = {}
    for d in a.labels:
        p = Path(d)
        rows = [json.loads(l) for l in (gzip.open(p, "rt") if p.suffix == ".gz" else open(p / "labels.jsonl"))]
        core, kind = rows[0]["core"], rows[0]["kind"]
        loc = json.loads((ROOT / "data" / "mutants" / f"{core}_{kind}_locations.json").read_text())["locations"]
        killed = collections.defaultdict(set)      # mutant -> profiles that kill it
        split, progs = {}, collections.Counter()
        for r in rows:
            split[r["mutant"]] = r["split"]
            if r["killed"]:
                killed[r["mutant"]].add(r["profile"])
        per = collections.defaultdict(lambda: collections.Counter())
        tot = collections.defaultdict(collections.Counter)
        tgt = set(targets.get(core, {}))

        def add(c, m):
            c["n"] += 1
            c["a"] += a.a in killed[m]
            c["b"] += a.b in killed[m]
            c["a_only"] += a.a in killed[m] and a.b not in killed[m]
            c["b_only"] += a.b in killed[m] and a.a not in killed[m]

        for m, s in split.items():
            mods = set(loc.get(str(m), {}).get("modules") or ["?"])
            for mod in mods:
                add(per[(mod, s)], m)
            add(tot[s], m)                          # totals count each mutant once
            add(tot[f"{s}-{'targeted' if mods & tgt else 'other'}"], m)
        print(f"== {core} {kind}: {len(split)} mutants; columns n / {a.a} / {a.b} / {a.a}-only / {a.b}-only")
        for (mod, s), c in sorted(per.items(), key=lambda x: (-x[1]["n"], x[0])):
            if c["n"] >= 3 or mod in tgt:
                print(f"  {mod:<28}{s:<6}{'T' if mod in tgt else ' '} {c['n']:>4} {c['a']:>4} {c['b']:>4} {c['a_only']:>4} {c['b_only']:>4}")
        for g, c in sorted(tot.items()):
            print(f"  TOTAL {g:<22} {c['n']:>4} {c['a']:>4} {c['b']:>4} {c['a_only']:>4} {c['b_only']:>4}")
        out[f"{core}_{kind}"] = {g: dict(c) for g, c in tot.items()}
        out[f"{core}_{kind}"]["modules"] = {f"{m}|{s}": dict(c) for (m, s), c in per.items()}
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
