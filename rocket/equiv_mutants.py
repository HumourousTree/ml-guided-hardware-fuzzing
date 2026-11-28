#!/usr/bin/env python3
"""Formal equivalence of Encarsia-style mutants against their reference (#7: which unkilled mutants are equivalent?).

  python3 rocket/equiv_mutants.py --dir MUTANT_DIR [--dir ...] [--seq 5] [--timeout 900] [--json out.json]

MUTANT_DIR holds host_driver.rtlil + reference_driver.rtlil (signal mix-up) or host_amt.rtlil with ../reference.rtlil
(broken conditional), the layout of Encarsia's output and of rocket/mutant_factory.py.

The injector changes one module and marks the change with a `buggy` attribute. Only that module is compared (with its
submodules flattened), port for port: equiv_make matches its wires and registers by name, equiv_simple proves each
matched point from its cone over --seq cycles, and equiv_induct closes the rest by temporal induction. If every point is
proven the mutated module computes the same outputs as the reference from the same state for every input sequence, so
the whole core is equivalent: the mutant can never be killed and is removed from the denominator.

This is sound in one direction only. An unproven module is not shown to differ at the core's outputs (the difference
may be masked by the rest of the core, or the proof may be too weak): such mutants are reported as "unproven", not as
observable. Encarsia proved observability with a commercial tool (JasperGold cover on a miter); a bounded
counterexample search for the unproven mutants is the other half of #7.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def buggy_module(rtlil: Path) -> str:
    mod = None
    with open(rtlil) as f:
        for line in f:
            if line.startswith("module "):
                mod = line.split()[1]
            elif "buggy" in line and line.lstrip().startswith("attribute"):
                return mod
    raise ValueError(f"no buggy attribute in {rtlil}")


def files(d: Path):
    if (d / "host_driver.rtlil").exists():
        return d / "host_driver.rtlil", d / "reference_driver.rtlil"
    return d / "host_amt.rtlil", d.parent / "reference.rtlil"


SCRIPT = """
read_rtlil {ref}
hierarchy -top {mod}
flatten
rename {mod} gold
design -stash gold
read_rtlil {host}
hierarchy -top {mod}
flatten
rename {mod} gate
design -stash gate
design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
memory_map
opt_clean
equiv_make gold gate equiv
hierarchy -top equiv
equiv_simple -seq {seq}
equiv_induct -seq {seq}
equiv_status
"""


def check(d: Path, seq: int, timeout: int, yosys: str = "yosys") -> dict:
    host, ref = files(d)
    mod = buggy_module(host)
    with tempfile.TemporaryDirectory() as w:
        # Every $pmux becomes a first-match $mux chain, the reading the simulators use (rocket/pmux_first_match.py).
        # Yosys SAT lets the last active select win instead; with that reading a broken conditional whose pattern
        # overlaps another entry was "proven" equivalent although JasperGold had covered it (published Rocket
        # multiplexer bugs 66 and 408). Broken conditionals ($amt cells) are lowered to $pmux first (lower_amt.py).
        for src, tag in ((host, "host"), (ref, "ref")):
            cur = src
            if src.name.endswith("amt.rtlil") or tag == "ref" and host.name == "host_amt.rtlil":
                subprocess.run([sys.executable, str(ROOT / "rocket" / "lower_amt.py"), str(cur),
                                str(Path(w) / f"{tag}_l.rtlil")], check=True, capture_output=True, timeout=timeout)
                cur = Path(w) / f"{tag}_l.rtlil"
            subprocess.run([sys.executable, str(ROOT / "rocket" / "pmux_first_match.py"), str(cur),
                            str(Path(w) / f"{tag}_fm.rtlil")], check=True, capture_output=True, timeout=timeout)
        host, ref = Path(w) / "host_fm.rtlil", Path(w) / "ref_fm.rtlil"
        ys = Path(w) / "eq.ys"
        ys.write_text(SCRIPT.format(ref=ref, host=host, mod=mod, seq=seq))
        t0 = time.time()
        try:
            r = subprocess.run([yosys, "-q", "-l", str(Path(w) / "log"), "-s", str(ys)], capture_output=True,
                               text=True, timeout=timeout)
            log = (Path(w) / "log").read_text(errors="replace")
        except subprocess.TimeoutExpired:
            return {"dir": str(d), "module": mod, "verdict": "timeout", "seconds": timeout}
    sec = round(time.time() - t0, 1)
    if r.returncode:
        return {"dir": str(d), "module": mod, "verdict": "error", "seconds": sec, "stderr": r.stderr[-800:]}
    m = re.findall(r"Found (\d+) \$equiv cells in equiv:\s*\n\s*Of those cells (\d+) are proven and (\d+) are unproven",
                   log)
    if not m:
        return {"dir": str(d), "module": mod, "verdict": "error", "seconds": sec, "log_tail": log[-800:]}
    total, proven, unproven = map(int, m[-1])
    return {"dir": str(d), "module": mod, "verdict": "equivalent" if unproven == 0 else "unproven",
            "equiv_cells": total, "unproven_cells": unproven, "seconds": sec}


def unkilled(core: str, kind: str, man: dict) -> list:
    """Manifest mutants that no program in any stored label file kills (data/labels, results/v0.9/templates/labels)."""
    import gzip
    hit = set()
    for f in list((ROOT / "data" / "labels").glob("*labels.jsonl.gz")) + \
            list((ROOT / "results" / "v0.9" / "templates" / "labels").glob("*labels.jsonl.gz")):
        for l in gzip.open(f, "rt"):
            r = json.loads(l)
            if r.get("killed") and r["core"] == core and r["kind"] == kind:
                hit.add(str(r["mutant"]))
    return [m for m in man["mutants"] if str(m["dir"]) not in hit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", default=[])
    ap.add_argument("--core", help="manifest mode: the unkilled mutants of data/mutants/<core>_<kind>_manifest.json")
    ap.add_argument("--kind")
    ap.add_argument("--published", action="store_true", help="the published Encarsia bugs instead (soundness check)")
    ap.add_argument("--mutants", default=str(Path.home() / "ext" / "mutants"), help="where the mutants are regenerated")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--seq", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--yosys", default="yosys")
    ap.add_argument("--json")
    a = ap.parse_args()
    todo = [(Path(d), {}) for d in a.dir]
    if a.core and a.published:     # soundness check: Encarsia proved every published bug observable
        import sys
        sys.path.insert(0, str(ROOT / "rocket"))
        import mutant_factory
        pub = json.loads((ROOT / "docs" / "encarsia_published.json").read_text())[a.core][f"{a.kind}_dirs"]
        mine = [d for i, d in enumerate(sorted(pub)) if i % a.of == a.shard]
        todo += [(mutant_factory.CORPUS / a.core / a.kind / str(d),
                  {"core": a.core, "kind": a.kind, "mutant": d, "split": "published"}) for d in mine]
    elif a.core:
        import sys
        sys.path.insert(0, str(ROOT / "rocket"))
        import mutant_factory
        man = json.loads((ROOT / "data" / "mutants" / f"{a.core}_{a.kind}_manifest.json").read_text())
        sel = unkilled(a.core, a.kind, man)
        mine = [m for i, m in enumerate(sel) if i % a.of == a.shard]
        print(f"{len(sel)} unkilled mutants, {len(mine)} in shard {a.shard}/{a.of}", flush=True)
        mdir = Path(a.mutants) / a.core / a.kind
        if not any(mdir.glob("*/" + mutant_factory.HOST_FILE[a.kind])):
            mutant_factory.inject(mutant_factory.INJECTOR[a.kind], a.core, a.kind, man["num_bugs_requested"], mdir)
        for m in mine:
            if mutant_factory.sha(mdir / str(m["dir"]) / mutant_factory.HOST_FILE[a.kind]) != m["sha256"]:
                raise SystemExit(f"mutant {m['dir']}: regenerated netlist differs from the manifest")
            todo.append((mdir / str(m["dir"]), {"core": a.core, "kind": a.kind, "mutant": m["dir"], "split": m["split"]}))
    out = []
    for d, meta in todo:
        try:
            r = {**meta, **check(d, a.seq, a.timeout, a.yosys)}
        except Exception as e:      # recorded, never silently dropped
            r = {**meta, "dir": str(d), "verdict": "error", "stderr": str(e)[-500:]}
        print(json.dumps(r), flush=True)
        out.append(r)
        if a.json:
            Path(a.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
