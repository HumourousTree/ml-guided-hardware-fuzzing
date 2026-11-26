#!/usr/bin/env python3
"""Training-mutant factory for the bug-trained generator (Track 1): Encarsia-style bugs in Rocket / BOOM, held out
from the 60 benchmark bugs.

  python3 rocket/mutant_factory.py --core rocket --kind driver [--num-bugs 1000] [--sample 400] [--seed 1]
                                   [--yosys /opt/encarsia-yosys/bin/yosys] [--out ~/ext/mutants]

1. Injection with Encarsia's own Yosys passes (encarsia-yosys, pinned in docker/encarsia/Dockerfile) on the same
   reference.v as EnCorpus: `inject_driver` (signal mix-ups) or `inject` (broken conditionals), with the script that
   encarsia-meta's host.py uses. Output: one directory per candidate bug, in the formats rocket/build_dut.sh reads
   (driver: host_driver.rtlil + reference_driver.rtlil; multiplexer: host_amt.rtlil + ../reference.rtlil).
2. Held-out rule: a candidate is excluded from training if its directory number is one of the 15 published bugs of its
   core and kind (docs/encarsia_published.json) or if its host netlist is byte-identical to a published bug's.
3. A reproducible random sample of the remaining candidates (--sample, --seed) is written to a manifest:
   <out>/<core>_<kind>_manifest.json with core, kind, dir, sha256 of the host netlist and the split (train / dev).
Encarsia additionally proves each bug observable with a commercial formal tool; we do not, so mutants that no program
exposes simply become negative examples in the labelling step (rocket/label_mutants.py).
"""
import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path.home() / "ext" / "encarsia" / "out"
HOST_MODULE = {"rocket": "Rocket", "boom": "BoomCore"}
HOST_FILE = {"driver": "host_driver.rtlil", "multiplexer": "host_amt.rtlil"}


def inject(yosys: str, core: str, kind: str, num_bugs: int, outdir: Path) -> float:
    """Run Encarsia's injection pass; returns seconds."""
    outdir.mkdir(parents=True, exist_ok=True)
    ref = CORPUS / core / "reference.v"
    if kind == "driver":
        pass_cmd = f"inject_driver -output-dir {outdir} -num-bugs {num_bugs}"
    else:
        pass_cmd = f"inject -output-dir {outdir}"
    script = outdir / "inject.ys"
    script.write_text("\n".join([
        f"read_verilog -sv {ref}",
        f"hierarchy -check -top {HOST_MODULE[core]}",
        "setattr -unset always_comb p:*",
        "proc -norom",
        pass_cmd, ""]))
    t0 = time.time()
    # unlimited stack: the conditional pass recurses over the whole core and overflows the default 8 MiB
    r = subprocess.run(["bash", "-c", 'ulimit -s unlimited; exec "$0" -q -l "$1" -s "$2"', yosys,
                        str(outdir / "inject.log"), str(script)], capture_output=True, text=True)
    if r.returncode:
        log = (outdir / "inject.log").read_text(errors="replace") if (outdir / "inject.log").exists() else ""
        raise RuntimeError(f"injection failed (exit {r.returncode}): {r.stderr[-1500:]} {r.stdout[-1500:]} "
                           f"log tail: {log[-2500:]}")
    return time.time() - t0


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", choices=list(HOST_MODULE), required=True)
    ap.add_argument("--kind", choices=list(HOST_FILE), required=True)
    ap.add_argument("--num-bugs", type=int, default=1000, help="candidates to inject (driver pass; the conditional "
                                                                 "pass uses its own default)")
    ap.add_argument("--sample", type=int, default=400, help="training + dev mutants to keep")
    ap.add_argument("--dev-frac", type=float, default=.15)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--yosys", default="/opt/encarsia-yosys/bin/yosys")
    ap.add_argument("--out", default=str(Path.home() / "ext" / "mutants"))
    a = ap.parse_args()
    out = Path(a.out) / a.core / a.kind
    secs = 0.0
    if not any(out.glob("*/" + HOST_FILE[a.kind])):
        secs = inject(a.yosys, a.core, a.kind, a.num_bugs, out)
    cands = sorted(int(p.name) for p in out.iterdir() if p.is_dir() and p.name.isdigit()
                   and (p / HOST_FILE[a.kind]).exists())
    pub = json.loads((ROOT / "docs" / "encarsia_published.json").read_text())[a.core]
    pub_dirs = set(pub["driver_dirs"] if a.kind == "driver" else pub["multiplexer_dirs"])
    pub_hashes = {sha(CORPUS / a.core / a.kind / str(d) / HOST_FILE[a.kind]) for d in pub_dirs
                  if (CORPUS / a.core / a.kind / str(d) / HOST_FILE[a.kind]).exists()}
    held, keep = [], []
    for d in cands:
        h = sha(out / str(d) / HOST_FILE[a.kind])
        (held if d in pub_dirs or h in pub_hashes else keep).append({"dir": d, "sha256": h,
                                                                     "same_dir_as_published": d in pub_dirs,
                                                                     "same_netlist_as_published": h in pub_hashes})
    # Encarsia keeps only candidates that a commercial formal tool proves observable; we cannot, so we (1) keep one
    # candidate per distinct netlist here and (2) let labelling decide: a mutant no pool program kills is reported as
    # "unkilled (possibly equivalent)", never as a bug.
    seen, uniq = set(), []
    for m in keep:
        if m["sha256"] not in seen:
            seen.add(m["sha256"]); uniq.append(m)
    dups = len(keep) - len(uniq)
    keep = uniq
    rng = random.Random(a.seed)
    sample = rng.sample(keep, min(a.sample, len(keep)))       # random order: the first dev_frac become the dev split
    n_dev = int(round(len(sample) * a.dev_frac))
    for i, m in enumerate(sample):
        m["split"] = "dev" if i < n_dev else "train"
    manifest = {"core": a.core, "kind": a.kind, "injector": "encarsia-yosys 24b50664c2",
                "num_bugs_requested": a.num_bugs, "candidates": len(cands), "duplicate_netlists_dropped": dups,
                "distinct_candidates": len(keep), "inject_seconds": round(secs, 1),
                "held_out": held, "held_out_published_matched": sum(1 for m in held if m["same_netlist_as_published"]),
                "seed": a.seed, "mutants": sorted(sample, key=lambda m: m["dir"])}
    mf = Path(a.out) / f"{a.core}_{a.kind}_manifest.json"
    mf.write_text(json.dumps(manifest, indent=1))
    print(f"{a.core} {a.kind}: {len(cands)} candidates in {secs:.0f} s; held out {len(held)} "
          f"({manifest['held_out_published_matched']} byte-identical to a published bug); {dups} duplicate netlists "
          f"dropped; sampled {len(sample)} "
          f"({sum(1 for m in sample if m['split'] == 'dev')} dev) -> {mf}")


if __name__ == "__main__":
    main()
