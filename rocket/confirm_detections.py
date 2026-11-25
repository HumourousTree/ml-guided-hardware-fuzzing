#!/usr/bin/env python3
"""Confirm every detection: replay the first detecting program of each run on the bug's own golden reference
(run_encarsia.golden_name: the bug's reference_driver.rtlil, or the multiplexer reference) and on the buggy netlist.
A detection counts as confirmed when the golden core matches Spike and the buggy one does not.

  python3 rocket/confirm_detections.py [--core rocket|boom] [--jobs 6] [--redo]
      -> logs/bench/<core>/confirmations.json  {file: {...}}   (resumable; --redo replays everything again)
"""
import argparse
import json
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
import rv64fuzz  # noqa: E402
import run_encarsia  # noqa: E402

NAMES = ([f"x{i}" for i in range(1, 31)] + [f"f{i}" for i in range(32)] + ["fcsr", "mscratch"]
         + ["mstatus", "mepc", "mcause", "mtval", "mie"])          # (data dwords follow; ext only has the 5 CSRs)


def name(i: int, ext: bool) -> str:
    n = NAMES if ext else NAMES[:64]
    return n[i] if i < len(n) else f"data{i - len(n)}"


def replay(core: str, path: str) -> tuple[str, dict]:
    runs = run_encarsia.paths(core)[1]
    f = Path(path)
    r = json.loads(f.read_text())
    fd = r["first_detection"]
    ext = r.get("profile") == "ext"
    golden = run_encarsia.golden_name(core, r["kind"], r["dir"])
    with tempfile.TemporaryDirectory() as w:
        elf = rv64fuzz.build_elf(fd["asm"], Path(w))
        exp = rv64fuzz.run_spike(elf)
        gold, gstop = rv64fuzz.run_rocket(runs / golden / "Vtop_tiny_soc", elf, 400000)
        bug, bstop = rv64fuzz.run_rocket(runs / f"{r['kind']}_{r['dir']}" / "Vtop_tiny_soc", elf, 400000)
    diffs = [name(i, ext) for i, (a, b) in enumerate(zip(bug, exp)) if a != b]
    v = {"golden": golden, "golden_matches_spike": bool(gstop and gold == exp),
         "bug_differs": bool((not bstop) or bug != exp),
         "kind": "nontermination" if not bstop else "mismatch", "differing": diffs[:6]}
    v["confirmed"] = v["golden_matches_spike"] and v["bug_differs"]
    return f.name, v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", choices=run_encarsia.CORES, default="rocket")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--redo", action="store_true", help="replay every detection again (e.g. after adding references)")
    a = ap.parse_args()
    out = run_encarsia.paths(a.core)[2]
    dest = out / "confirmations.json"
    done = {} if a.redo or not dest.exists() else json.loads(dest.read_text())
    todo = [str(f) for f in sorted(out.glob("*_seed*.json"))
            if f.name not in done and json.loads(f.read_text()).get("first_detection")]
    with ProcessPoolExecutor(a.jobs) as ex:
        for fu in as_completed([ex.submit(replay, a.core, t) for t in todo]):
            n, v = fu.result()
            done[n] = v
            print(n, v, flush=True)
            dest.write_text(json.dumps(done, indent=1))
    bad = [k for k, v in done.items() if not v["confirmed"]]
    print(f"{len(done)} detections replayed, {len(done) - len(bad)} confirmed" + (f"; NOT confirmed: {bad}" if bad else ""))


if __name__ == "__main__":
    main()
