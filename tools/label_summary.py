"""Summarise a kill matrix (rocket/label_mutants.py output) for Track 1.

  python tools/label_summary.py labels.jsonl [more.jsonl ...] [--json out.json]

Per file (core x kind): mutants labelled, build errors, mutants killed by any program / by none ("unkilled, possibly
equivalent"), per-profile mutant coverage (share of mutants killed by at least one program of that profile),
mutants only one profile kills, how detections split by kind (end-state mismatch vs non-termination), and the
same numbers for the train and dev splits.
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def summarise(path: Path) -> dict:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    lab = [r for r in rows if "killed" in r]
    errors = sorted({r["mutant"] for r in rows if "error" in r})
    core, kind = (lab[0]["core"], lab[0]["kind"]) if lab else ("?", "?")
    by_mut = collections.defaultdict(lambda: collections.defaultdict(list))
    split = {}
    how = collections.Counter()
    for r in lab:
        by_mut[r["mutant"]][r["profile"]].append(bool(r["killed"]))
        split[r["mutant"]] = r["split"]
        if r["killed"]:
            how[r.get("how") or "mismatch"] += 1
    profiles = sorted({p for m in by_mut.values() for p in m})

    def stats(muts):
        n = len(muts)
        killed = [m for m in muts if any(any(v) for v in by_mut[m].values())]
        cover = {p: sum(1 for m in muts if any(by_mut[m].get(p, []))) for p in profiles}
        only = {p: sum(1 for m in muts if any(by_mut[m].get(p, []))
                       and not any(any(by_mut[m].get(q, [])) for q in profiles if q != p)) for p in profiles}
        rate = {p: round(sum(sum(by_mut[m].get(p, [])) for m in muts) /
                         max(1, sum(len(by_mut[m].get(p, [])) for m in muts)), 4) for p in profiles}
        return {"mutants": n, "killed_by_any": len(killed), "unkilled_possibly_equivalent": n - len(killed),
                "coverage_by_profile": cover, "only_this_profile": only, "program_kill_rate": rate}

    all_m = sorted(by_mut)
    return {"core": core, "kind": kind, "file": str(path), "rows": len(lab), "build_errors": len(errors),
            "detections_by_how": dict(how), "all": stats(all_m),
            "train": stats([m for m in all_m if split[m] == "train"]),
            "dev": stats([m for m in all_m if split[m] == "dev"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    out = [summarise(f) for f in a.files]
    for s in out:
        x = s["all"]
        print(f"{s['core']} {s['kind']}: {x['mutants']} mutants ({s['build_errors']} build errors), killed by any "
              f"{x['killed_by_any']}, unkilled {x['unkilled_possibly_equivalent']}")
        print("   coverage by profile:", x["coverage_by_profile"], " only this profile:", x["only_this_profile"])
        print("   program kill rate:", x["program_kill_rate"], " detections:", s["detections_by_how"])
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
