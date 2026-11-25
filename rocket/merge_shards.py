#!/usr/bin/env python3
"""Merge cloud shards (rocket/cloud_shard.py) into logs/bench/<core>/ and check them.

  python3 rocket/merge_shards.py --core rocket|boom --profile base|ext <shard_dir> [<shard_dir> ...]

Each shard_dir contains the files a shard wrote to logs/bench/<core>/ (e.g. downloaded GitHub Actions artifacts).
Run files and calibrations are copied (existing files are never overwritten with different content); the per-shard
replay files are merged into confirmations.json. Exits non-zero if any reference calibration has a mismatch or a
non-terminating program, or if a bug's reference was not calibrated, because then that bug's results are not valid.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
import run_encarsia as re_  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", choices=re_.CORES, required=True)
    ap.add_argument("--profile", choices=["base", "ext", "llm", "llmflash"], default="base")
    ap.add_argument("dirs", nargs="+")
    a = ap.parse_args()
    out = re_.paths(a.core)[2]
    out.mkdir(parents=True, exist_ok=True)
    sfx = re_.suffix(a.profile)
    conf_f = out / "confirmations.json"
    conf = json.loads(conf_f.read_text()) if conf_f.exists() else {}
    copied, clashes = 0, []
    for d in map(Path, a.dirs):
        for f in sorted(d.rglob("*.json")):
            if f.name.startswith("confirmations_shard"):
                conf.update(json.loads(f.read_text()))
                continue
            dst = out / f.name
            if dst.exists() and dst.read_bytes() != f.read_bytes():
                clashes.append(f.name)
                continue
            shutil.copy2(f, dst); copied += 1
    conf_f.write_text(json.dumps(conf, indent=1))
    # validity: every reference used by a run of this profile must be calibrated without a single disagreement
    bad, missing = [], set()
    for g in sorted(out.glob(f"golden_*{sfx}.json")):
        if (a.profile == "ext") != g.name.endswith("_ext.json"):
            continue
        r = json.loads(g.read_text())
        if r["mismatches"] or r["nonterminating"]:
            bad.append(g.name)
    for f in sorted(out.glob(f"*_seed?{sfx}.json")):
        r = json.loads(f.read_text())
        g = re_.golden_name(a.core, r["kind"], r["dir"])
        if not (out / f"{g}{sfx}.json").exists():
            missing.add(g)
    runs = list(out.glob(f"*_seed?{sfx}.json"))
    det = [f.name for f in runs if json.loads(f.read_text()).get("first_detection")]
    print(f"{a.core} {a.profile}: {copied} files merged, {len(runs)} runs, {len(det)} detections, "
          f"{sum(1 for n in det if conf.get(n, {}).get('confirmed'))} replay-confirmed, "
          f"{sum(1 for n in det if n not in conf)} not replayed")
    if clashes:
        print("files with different content left untouched:", clashes)
    if bad or missing:
        sys.exit(f"INVALID: failed calibrations {bad}; uncalibrated references {sorted(missing)}")


if __name__ == "__main__":
    main()
