"""Bug-targeted generation (Track 1, round 4): do programs sampled for a mutant's module kill it more often than
programs from the same model sampled without a module?

  python tools/genmodel_targeted.py --targeted t.jsonl[.gz] ... --untargeted u.jsonl[.gz] ... [--json out.json]

Targeted rows come from `genmodel-eval.yml match_class=true` (profile samples_<preset>_dpo@<class>_<core>_<prof>; each
program labelled only against the mutants of its class), untargeted rows from the same model without the module token
(profile samples_<preset>_dpo_<core>_<prof>). Per mutant and generator profile, kill rate = killing programs / programs.

* Paired test over (mutant, profile) cells that either side kills: mean difference of kill rates, 95% bootstrap
  interval over mutants (2000 draws), and a sign test (targeted higher vs lower).
* Equal-size coverage: mutants killed by n programs per profile (n = smallest targeted pool), 2000 random draws on both
  sides, per split.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import os
import re

import numpy as np

T_RE = re.compile(r"samples_.+?@(.+?)_(rocket|boom)_(base|ext|llmflash)$")
U_RE = re.compile(r"samples_.+?_(rocket|boom)_(base|ext|llmflash)$")


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


def table(paths, targeted):
    """{(split, kind, mutant): {profile: {program: killed}}}, plus the class of each targeted mutant."""
    out = collections.defaultdict(lambda: collections.defaultdict(dict))
    cls = {}
    for r in rows(paths):
        prof = r["profile"]
        if targeted:
            m = T_RE.match(prof)
            if not m:
                continue
            c, p = m.group(1), m.group(3)
        else:
            if "@" in prof:
                continue
            m = U_RE.match(prof)
            if not m:
                continue
            c, p = None, m.group(2)
        key = (r["split"], r["kind"], str(r["mutant"]))
        out[key][p][r["program"]] = bool(r["killed"])
        if c:
            cls[key] = c
    return out, cls


def sign_p(k, n):
    """Two-sided exact binomial p for k of n at 1/2."""
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targeted", nargs="+", required=True); ap.add_argument("--untargeted", nargs="+", required=True)
    ap.add_argument("--draws", type=int, default=2000); ap.add_argument("--seed", type=int, default=1); ap.add_argument("--json")
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    T, cls = table(a.targeted, True)
    U, _ = table(a.untargeted, False)
    common = sorted(k for k in T if k in U)
    res = {"mutants": len(common), "splits": {}}
    for split in sorted({k[0] for k in common}):
        keys = [k for k in common if k[0] == split]
        cells = []          # (mutant index, profile, targeted rate, untargeted rate)
        for i, k in enumerate(keys):
            for p in sorted(set(T[k]) & set(U[k])):
                t = np.mean(list(T[k][p].values())); u = np.mean(list(U[k][p].values()))
                if t > 0 or u > 0:
                    cells.append((i, p, t, u))
        d = np.array([c[2] - c[3] for c in cells]); mi = np.array([c[0] for c in cells])
        hi_ = int((d > 0).sum()); lo_ = int((d < 0).sum())
        boot = []
        um = np.unique(mi)
        for _ in range(a.draws):
            pick = rng.choice(um, len(um))
            boot.append(np.concatenate([d[mi == j] for j in pick]).mean() if len(pick) else 0.0)
        blo, bhi = np.percentile(boot, [2.5, 97.5]) if len(d) else (0.0, 0.0)
        t_rate = np.mean([c[2] for c in cells]) if cells else 0.0; u_rate = np.mean([c[3] for c in cells]) if cells else 0.0
        # equal-size coverage: n programs per profile, n = smallest targeted pool for any mutant/profile
        n = min(len(T[k][p]) for k in keys for p in T[k])
        def cover(side):
            tot = []
            for _ in range(a.draws // 4):
                c = 0
                for k in keys:
                    hit = False
                    for p, progs in side[k].items():
                        v = np.array(list(progs.values()))
                        if v[rng.choice(len(v), min(n, len(v)), replace=False)].any():
                            hit = True; break
                    c += hit
                tot.append(c)
            return np.mean(tot), *np.percentile(tot, [2.5, 97.5])
        ct, cu = cover(T), cover(U)
        full_t = sum(any(any(v.values()) for v in T[k].values()) for k in keys)
        full_u = sum(any(any(v.values()) for v in U[k].values()) for k in keys)
        by_cls = collections.defaultdict(list)
        for c in cells:
            by_cls[cls[keys[c[0]]]].append(c[2] - c[3])
        print(f"== {split}: {len(keys)} mutants with a module class, {len(cells)} (mutant, profile) cells killed by either side")
        print(f"   mean kill rate  targeted {t_rate:.4f}  untargeted {u_rate:.4f}  diff {d.mean():+.4f} [{blo:+.4f}, {bhi:+.4f}]")
        print(f"   cells targeted higher {hi_}, lower {lo_}, equal {len(d) - hi_ - lo_}; sign test p = {sign_p(hi_, hi_ + lo_):.3f}")
        print(f"   mutants killed, all programs: targeted {full_t}, untargeted {full_u}; "
              f"{n}/profile: targeted {ct[0]:.1f} [{ct[1]:.0f}, {ct[2]:.0f}], untargeted {cu[0]:.1f} [{cu[1]:.0f}, {cu[2]:.0f}]")
        print("   by class (mean diff, cells): " + ", ".join(f"{c} {np.mean(v):+.3f} ({len(v)})" for c, v in sorted(by_cls.items())))
        res["splits"][split] = {"mutants": len(keys), "cells": len(cells), "targeted_rate": float(t_rate),
                                "untargeted_rate": float(u_rate), "diff": [float(d.mean()), float(blo), float(bhi)],
                                "higher": hi_, "lower": lo_, "sign_p": sign_p(hi_, hi_ + lo_), "pool_per_profile": n,
                                "killed_all": [full_t, full_u], "killed_equal": [list(map(float, ct)), list(map(float, cu))],
                                "by_class": {c: [float(np.mean(v)), len(v)] for c, v in by_cls.items()}}
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
