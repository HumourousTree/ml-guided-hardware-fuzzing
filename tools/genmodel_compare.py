"""Learned generator vs the generator pools, per model stage (Track 1 evaluation).

  python tools/genmodel_compare.py --model labels.jsonl [published_model.jsonl ...] --baseline base.jsonl [...] [--json out.json]

Model rows have profile = sample-file stem (samples_<preset>_<tag>_<core>_<profile>); they are grouped by
<preset>_<tag> (e.g. small_dpo), the union over its profiles. Baseline rows (base/ext/llmflash) form one group,
"generators". For each (kind, split) the table gives mutants killed by each group, out of the mutants both sides
labelled, and the kills only that group finds. Equal pool sizes are the caller's job (genmodel-eval.yml: 120 per file).
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import re


def rows(paths):
    for p in paths:
        if not os.path.exists(p):                  # a shard that failed: its rows are simply absent
            continue
        with (gzip.open(p, "rt") if p.endswith(".gz") else open(p)) as f:
            for l in f:
                if l.strip():
                    r = json.loads(l)
                    if "error" not in r:
                        yield r


def group(profile: str) -> str:
    m = re.match(r"samples_(.+?)_(rocket|boom)_(base|ext|llmflash)$", profile)
    return m.group(1) if m else "generators"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", nargs="+", required=True); ap.add_argument("--baseline", nargs="+", required=True)
    ap.add_argument("--json")
    a = ap.parse_args()
    killed = collections.defaultdict(set)          # (kind, split, group) -> mutants killed
    seen = collections.defaultdict(set)            # (kind, split, side) -> mutants labelled
    progs = collections.defaultdict(set)
    for side, paths in (("model", a.model), ("baseline", a.baseline)):
        for r in rows(paths):
            g = group(r["profile"]) if side == "model" else "generators"
            key = (r["kind"], r["split"])
            seen[key + (side,)].add(r["mutant"]); progs[key + (g,)].add(r["program"])
            if r["killed"]:
                killed[key + (g,)].add(r["mutant"])
    out = {}
    for kind, split in sorted({k[:2] for k in seen}):
        common = seen[(kind, split, "model")] & seen[(kind, split, "baseline")]
        if not common:
            continue
        groups = sorted({k[2] for k in killed if k[:2] == (kind, split)} | {"generators"})
        gen = killed[(kind, split, "generators")] & common
        print(f"== {kind} / {split}: {len(common)} mutants labelled by both")
        print(f"   {'group':<22}{'programs':>9}{'killed':>8}{'only this':>11}  (vs generators)")
        res = {}
        for g in groups:
            k = killed[(kind, split, g)] & common
            only = sorted(k - gen) if g != "generators" else []
            print(f"   {g:<22}{len(progs[(kind, split, g)]):>9}{len(k):>8}{len(only) if g != 'generators' else '':>11}"
                  + (f"  missed {len(gen - k)}" if g != "generators" else ""))
            res[g] = {"programs": len(progs[(kind, split, g)]), "killed": len(k), "only": only,
                      "missed": sorted(gen - k) if g != "generators" else []}
        out[f"{kind}/{split}"] = {"mutants": len(common), "groups": res}
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
