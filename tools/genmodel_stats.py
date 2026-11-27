"""Kill statistics for the learned generator vs the generator pools (Track 1): uncertainty and pool-size curves.

  python tools/genmodel_stats.py --model a.jsonl[.gz] ... --baseline b.jsonl[.gz] ... [--split dev] [--json out.json]

Rows are grouped as in tools/genmodel_compare.py (model: <preset>_<tag> from the sample-file stem; baseline:
"generators"). Only mutants labelled by every group are used, both bug kinds pooled.

* Subsampling (--half programs per profile without replacement, 2000 draws): 95% interval of the mutants killed by
  a pool of that size from the same source, and of the difference to an independent generator pool of the same size.
  This is the variation from "which programs happened to be sampled"; the mutant set is fixed (it is the benchmark).
* Pool-size curve: mean mutants killed by n programs per profile (random subsets, 500 draws), n = 5..120.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import re

import numpy as np


def rows(paths):
    for p in paths:
        if not os.path.exists(p):
            continue
        with (gzip.open(p, "rt") if p.endswith(".gz") else open(p)) as f:
            for l in f:
                if l.strip():
                    r = json.loads(l)
                    if "killed" in r:
                        yield r


def group(profile: str) -> tuple[str, str]:
    m = re.match(r"samples_(.+?)_(rocket|boom)_(base|ext|llmflash)$", profile)
    return (m.group(1), m.group(3)) if m else ("generators", profile)


def matrices(model, baseline, split):
    kill = collections.defaultdict(dict)           # group -> {(program, profile): {mutant: killed}}
    seen = collections.defaultdict(set)
    for side, paths in (("m", model), ("b", baseline)):
        for r in rows(paths):
            if r["split"] != split:
                continue
            g, prof = group(r["profile"])
            if (side == "m") != (g != "generators"):     # model files: model rows only; baseline files: generator rows only
                continue
            mut = (r["kind"], str(r["mutant"]))
            kill[g].setdefault((r["program"], prof), {})[mut] = r["killed"]
            seen[g].add(mut)
    common = sorted(set.intersection(*seen.values()))
    out = {}
    for g, progs in kill.items():
        keys = sorted(progs)
        M = np.array([[progs[k].get(m, False) for m in common] for k in keys], dtype=bool)
        out[g] = (M, np.array([k[1] for k in keys]))
    return out, common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", nargs="+", required=True); ap.add_argument("--baseline", nargs="+", required=True)
    ap.add_argument("--split", default="dev"); ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--half", type=int, default=60, help="programs per profile in each subsample")
    ap.add_argument("--json"); ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    mats, common = matrices(a.model, a.baseline, a.split)
    print(f"== {a.split}: {len(common)} mutants labelled by every group ({sorted(collections.Counter(k for k, _ in common).items())})")
    # subsampling (without replacement, a.half programs per profile): with-replacement bootstrap undercounts coverage
    # (only ~63% distinct programs), so pools of equal size are compared instead
    boot = {}
    for g, (M, prof) in sorted(mats.items()):
        idx = [np.flatnonzero(prof == p) for p in sorted(set(prof))]
        n = min(a.half, *(len(i) for i in idx))
        boot[g] = np.array([M[np.concatenate([rng.choice(i, n, replace=False) for i in idx])].any(0).sum()
                            for _ in range(a.draws)])
    gen = boot["generators"]
    res = {"mutants": len(common), "subsample_per_profile": a.half, "groups": {}}
    print(f"   {'group':<20}{'progs':>6}{'killed':>8}{'kill rate':>11}   {f'killed by {a.half}/profile (95%)':<26}{'diff vs gen (95%)':>18}")
    for g, (M, prof) in sorted(mats.items()):
        k = int(M.any(0).sum()); lo, hi = np.percentile(boot[g], [2.5, 97.5])
        diff = boot[g] - rng.permutation(gen); dlo, dhi = np.percentile(diff, [2.5, 97.5])
        rate = M.mean()
        print(f"   {g:<20}{len(M):>6}{k:>8}{rate:>11.4f}   {f'{boot[g].mean():.1f} [{lo:.0f}, {hi:.0f}]':<26}"
              + (f"{f'{np.mean(diff):+.1f} [{dlo:+.0f}, {dhi:+.0f}]':>18}" if g != "generators" else ""))
        res["groups"][g] = {"programs": len(M), "killed": k, "kill_rate": float(rate),
                            "sub_killed": [float(boot[g].mean()), float(lo), float(hi)],
                            "diff_vs_generators": [float(np.mean(diff)), float(dlo), float(dhi)]}
    # pool-size curve
    sizes = [n for n in (5, 10, 20, 40, 80, 120)]
    print(f"   pool-size curve: mutants killed by n programs per profile (mean of 500 random subsets)")
    print(f"   {'group':<20}" + "".join(f"{n:>7}" for n in sizes))
    for g, (M, prof) in sorted(mats.items()):
        idx = [np.flatnonzero(prof == p) for p in sorted(set(prof))]
        row = []
        for n in sizes:
            if any(len(i) < n for i in idx):
                row.append(None); continue
            row.append(float(np.mean([M[np.concatenate([rng.choice(i, n, replace=False) for i in idx])].any(0).sum()
                                      for _ in range(500)])))
        res["groups"][g]["curve"] = dict(zip(sizes, row))
        print(f"   {g:<20}" + "".join(f"{v:>7.1f}" if v is not None else f"{'-':>7}" for v in row))
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
