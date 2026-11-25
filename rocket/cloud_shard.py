#!/usr/bin/env python3
"""One shard of an Encarsia campaign, self-contained, for parallel cloud runners (GitHub Actions, VMs).

  python3 rocket/cloud_shard.py --core rocket|boom --profile base|ext --shard K --of N
                                [--seeds 5] [--programs 2000] [--calibrate 500] [--jobs 4] [--list]

Work items (sorted, item i belongs to shard i mod N):
  cal:<golden>         build a bug-free reference and calibrate it against Spike (--calibrate programs)
  bug:<kind>:<dir>     build the bug and its own reference, fuzz all seeds, replay every detection on both
Results are written exactly as by run_encarsia.py / confirm_detections.py (logs/bench/<core>/), except that replays go to
confirmations_shard<K>.json; rocket/merge_shards.py combines shards and checks that every calibration passed.
A bug result is only valid if its reference's calibration passed; the merge step enforces this.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
import confirm_detections  # noqa: E402
import run_encarsia as re_  # noqa: E402


def items(core: str) -> list[str]:
    kinds = ["driver", "multiplexer"]
    cal = sorted(f"cal:{g}" for g in re_.golden_sources(core, kinds))
    bugs = [f"bug:{k}:{d}" for k in kinds for d in re_.bug_dirs(core, k)]
    return cal + bugs


def source(core: str, name: str) -> Path:
    """The netlist for one simulator (only that one is lowered, unlike run_encarsia.build_targets)."""
    corpus = re_.paths(core)[0]
    if name == "golden_multiplexer":
        return re_.lowered(core, corpus / "multiplexer" / "reference.rtlil", "reference")
    if name.startswith("golden_driver_"):
        return re_.golden_sources(core, ["driver"])[name]
    kind, d = name.rsplit("_", 1)
    if kind == "driver":
        return corpus / "driver" / d / "host_driver.rtlil"
    return re_.lowered(core, corpus / "multiplexer" / d / "host_amt.rtlil", d)


def build_one(core: str, name: str) -> None:
    re_.build(core, name, source(core, name))


def run_item(core: str, item: str, a) -> dict:
    t0 = time.time()
    out = re_.paths(core)[2]
    if item.startswith("cal:"):
        name = item[4:]
        build_one(core, name)
        r = re_.calibrate(core, name, a.calibrate, a.profile)
        return {"item": item, "programs": r["programs"], "mismatches": r["mismatches"],
                "nonterminating": r["nonterminating"], "seconds": round(time.time() - t0)}
    _, kind, d = item.split(":")
    d = int(d)
    build_one(core, f"{kind}_{d}")
    build_one(core, re_.golden_name(core, kind, d))
    with ProcessPoolExecutor(a.jobs) as ex:
        runs = list(ex.map(re_.fuzz, *zip(*[(core, kind, d, s, a.programs, a.profile) for s in range(a.seeds)])))
    sfx = re_.suffix(a.profile)
    conf = {}
    for s, r in enumerate(runs):
        if r.get("first_detection"):
            n, v = confirm_detections.replay(core, str(out / f"{kind}_{d}_seed{s}{sfx}.json"))
            conf[n] = v
    return {"item": item, "detected": sum(bool(r.get("first_detection")) for r in runs), "seeds": len(runs),
            "confirmations": conf, "seconds": round(time.time() - t0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", choices=re_.CORES, required=True)
    ap.add_argument("--profile", choices=["base", "ext", "llm", "llmflash"], default="base")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--programs", type=int, default=2000)
    ap.add_argument("--calibrate", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--list", action="store_true", help="print this shard's items and exit")
    a = ap.parse_args()
    mine = [it for i, it in enumerate(items(a.core)) if i % a.of == a.shard]
    print(f"shard {a.shard}/{a.of} ({a.core}, {a.profile}): {len(mine)} items: {mine}", flush=True)
    if a.list:
        return
    out = re_.paths(a.core)[2]
    out.mkdir(parents=True, exist_ok=True)
    summary, conf = [], {}
    for it in mine:
        r = run_item(a.core, it, a)
        conf.update(r.pop("confirmations", {}))
        summary.append(r)
        print(json.dumps(r), flush=True)
    (out / f"confirmations_shard{a.shard}.json").write_text(json.dumps(conf, indent=1))
    (out / f"shard{a.shard}_{a.profile}_summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
