#!/usr/bin/env python3
"""Confirm every Rocket detection: replay the first detecting program of each run on the golden reference (same kind) and
on the buggy netlist. A detection counts as confirmed when the golden Rocket matches Spike and the buggy one does not.

  python3 rocket/confirm_detections.py [jobs=6]  -> logs/bench/rocket/confirmations.json  {file: {...}}   (resumable)
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
import rv64fuzz  # noqa: E402

RUNS = Path.home() / "rocket_runs"
OUT = ROOT / "logs" / "bench" / "rocket"
NAMES = ([f"x{i}" for i in range(1, 31)] + [f"f{i}" for i in range(32)] + ["fcsr", "mscratch"]
         + ["mstatus", "mepc", "mcause", "mtval", "mie"])          # (data dwords follow; ext only has the 5 CSRs)


def name(i: int, ext: bool) -> str:
    n = NAMES if ext else NAMES[:64]
    return n[i] if i < len(n) else f"data{i - len(n)}"


def replay(path: str) -> tuple[str, dict]:
    f = Path(path)
    r = json.loads(f.read_text())
    fd = r["first_detection"]
    ext = r.get("profile") == "ext"
    with tempfile.TemporaryDirectory() as w:
        elf = rv64fuzz.build_elf(fd["asm"], Path(w))
        exp = rv64fuzz.run_spike(elf)
        gold, gstop = rv64fuzz.run_rocket(RUNS / f"golden_{r['kind']}" / "Vtop_tiny_soc", elf, 400000)
        bug, bstop = rv64fuzz.run_rocket(RUNS / f"{r['kind']}_{r['dir']}" / "Vtop_tiny_soc", elf, 400000)
    diffs = [name(i, ext) for i, (a, b) in enumerate(zip(bug, exp)) if a != b]
    v = {"golden_matches_spike": bool(gstop and gold == exp), "bug_differs": bool((not bstop) or bug != exp),
         "kind": "nontermination" if not bstop else "mismatch", "differing": diffs[:6]}
    v["confirmed"] = v["golden_matches_spike"] and v["bug_differs"]
    return f.name, v


def main():
    from concurrent.futures import ProcessPoolExecutor, as_completed
    dest = OUT / "confirmations.json"
    done = json.loads(dest.read_text()) if dest.exists() else {}
    todo = [str(f) for f in sorted(OUT.glob("*_seed*.json"))
            if f.name not in done and json.loads(f.read_text()).get("first_detection")]
    with ProcessPoolExecutor(int(sys.argv[1]) if len(sys.argv) > 1 else 6) as ex:
        for fu in as_completed([ex.submit(replay, t) for t in todo]):
            n, v = fu.result()
            done[n] = v
            print(n, v, flush=True)
            dest.write_text(json.dumps(done, indent=1))
    bad = [k for k, v in done.items() if not v["confirmed"]]
    print(f"{len(done)} detections replayed, {len(done) - len(bad)} confirmed" + (f"; NOT confirmed: {bad}" if bad else ""))


if __name__ == "__main__":
    main()
