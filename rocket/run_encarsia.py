#!/usr/bin/env python3
"""BenchFuzz on Encarsia's 30 Rocket bugs (15 signal mix-ups, 15 broken conditionals). Resumable.

  python3 rocket/run_encarsia.py [--seeds 5] [--programs 2000] [--jobs 6] [--build-jobs 2] [--only driver|multiplexer]

1. Builds one simulator per bug (rocket/build_dut.sh; broken conditionals first go through rocket/lower_amt.py) and the
   two golden references (driver reference, lowered multiplexer reference).
2. Calibrates each golden reference: --calibrate programs must give 0 mismatches against Spike, otherwise it stops.
3. Fuzzes every bug with --seeds independent seeds, up to --programs programs each, stopping at the first detection.
Results: logs/bench/rocket/<kind>_<dir>_seed<s>.json and logs/bench/rocket/golden_<kind>.json.
Bug k of the paper = k-th directory in ascending order (docs/encarsia_published.json).
"""
import argparse
import json
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
import rv64fuzz  # noqa: E402

CORPUS = Path.home() / "ext" / "encarsia" / "out" / "rocket"
RUNS = Path.home() / "rocket_runs"
OUT = ROOT / "logs" / "bench" / "rocket"


def bug_dirs(kind: str) -> list[int]:
    return sorted(int(p.name) for p in (CORPUS / kind).iterdir() if p.is_dir() and p.name.isdigit())


def rtlil_for(kind: str, d: int | None) -> Path:
    """The netlist to build; broken conditionals are lowered from $amt first."""
    if kind == "driver":
        return CORPUS / "driver" / (f"{d}/host_driver.rtlil" if d else "767/reference_driver.rtlil")
    src = CORPUS / "multiplexer" / (f"{d}/host_amt.rtlil" if d else "reference.rtlil")
    dst = RUNS / "lowered" / f"mux_{d or 'reference'}.rtlil"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(ROOT / "rocket" / "lower_amt.py"), str(src), str(dst)], check=True,
                       capture_output=True)
    return dst


def build(kind: str, d: int | None) -> Path:
    out = RUNS / (f"{kind}_{d}" if d else f"golden_{kind}")
    if not (out / "Vtop_tiny_soc").exists():
        r = subprocess.run(["bash", str(ROOT / "rocket" / "build_dut.sh"), str(rtlil_for(kind, d)), str(out)],
                           capture_output=True, text=True)
        if r.returncode or not (out / "Vtop_tiny_soc").exists():
            raise RuntimeError(f"build failed for {kind} {d}: {r.stdout[-300:]} {r.stderr[-300:]}")
    return out / "Vtop_tiny_soc"


def fuzz(kind: str, d: int, seed: int, programs: int) -> dict:
    f = OUT / f"{kind}_{d}_seed{seed}.json"
    if f.exists():
        return json.loads(f.read_text())
    sim = RUNS / f"{kind}_{d}" / "Vtop_tiny_soc"
    r = rv64fuzz.campaign(sim, programs, 1000 * seed + d, 40, 400000, True, stop_on_bug=True)
    r.update(kind=kind, dir=d, seed=seed, budget_programs=programs)
    f.write_text(json.dumps(r))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--programs", type=int, default=2000)
    ap.add_argument("--calibrate", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--build-jobs", type=int, default=2)
    ap.add_argument("--only", choices=["driver", "multiplexer"])
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    kinds = [a.only] if a.only else ["driver", "multiplexer"]
    # 1. builds (memory: ~3.5 GB each, hence few in parallel)
    todo = [(k, None) for k in kinds] + [(k, d) for k in kinds for d in bug_dirs(k)]
    with ProcessPoolExecutor(a.build_jobs) as ex:
        futs = {ex.submit(build, k, d): (k, d) for k, d in todo}
        for fu in as_completed(futs):
            k, d = futs[fu]
            try:
                fu.result(); print(f"built {k} {d or 'golden'}", flush=True)
            except Exception as e:
                print(f"BUILD FAILED {k} {d}: {e}", flush=True)
    # 2. golden calibration: a reference that disagrees with Spike would make every detection meaningless
    for k in kinds:
        g = OUT / f"golden_{k}.json"
        if not g.exists():
            r = rv64fuzz.campaign(RUNS / f"golden_{k}" / "Vtop_tiny_soc", a.calibrate, 777, 40, 400000, True, False)
            g.write_text(json.dumps(r))
        r = json.loads(g.read_text())
        print(f"golden {k}: {r['programs']} programs, {r['mismatches']} mismatches, {r['nonterminating']} non-terminating",
              flush=True)
        if r["mismatches"] or r["nonterminating"]:
            sys.exit(f"golden {k} disagrees with Spike: fix the harness before fuzzing bugs")
    # 3. fuzzing
    jobs = [(k, d, s) for k in kinds for d in bug_dirs(k) if (RUNS / f"{k}_{d}" / "Vtop_tiny_soc").exists()
            for s in range(a.seeds)]
    with ProcessPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(fuzz, k, d, s, a.programs): (k, d, s) for k, d, s in jobs}
        for fu in as_completed(futs):
            k, d, s = futs[fu]
            r = fu.result()
            fd = r.get("first_detection") or {}
            print(f"{k:11s} {d:5d} seed {s}: " + (f"DETECTED at program {fd['n_programs']} ({fd['kind']})" if fd
                  else f"not detected in {r['programs']} programs"), flush=True)


if __name__ == "__main__":
    main()
