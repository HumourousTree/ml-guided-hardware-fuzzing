#!/usr/bin/env python3
"""BenchFuzz on Encarsia's Rocket or BOOM bugs (15 signal mix-ups, 15 broken conditionals per core). Resumable.

  python3 rocket/run_encarsia.py [--core rocket|boom] [--seeds 5] [--programs 2000] [--jobs 6] [--build-jobs 2]
                                 [--only driver|multiplexer] [--profile base|ext] [--calibrate 500]

1. Builds one simulator per bug (rocket/build_dut.sh; broken conditionals first go through rocket/lower_amt.py) and the
   golden references: every *distinct* reference_driver.rtlil of the signal mix-ups (Encarsia prepares each mix-up from
   its own copy of the design; Rocket has 5 distinct ones, BOOM 14) and the lowered multiplexer reference.
2. Calibrates each golden reference: --calibrate programs must give 0 mismatches against Spike, otherwise it stops.
3. Fuzzes every bug with --seeds independent seeds, up to --programs programs each, stopping at the first detection.
Results: logs/bench/<core>/<kind>_<dir>_seed<s>.json and logs/bench/<core>/golden_<name>.json (profile "ext": the same
names with an "_ext" suffix; the ext profile adds machine-mode CSRs, exceptions and compressed instructions, see
rocket/rv64fuzz.py). Bug k of the paper = k-th directory in ascending order (docs/encarsia_published.json).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
import rv64fuzz  # noqa: E402

CORES = ("rocket", "boom")


def paths(core: str) -> tuple[Path, Path, Path]:
    """(EnCorpus directory, simulator directory, result directory) of a core."""
    return (Path.home() / "ext" / "encarsia" / "out" / core, Path.home() / f"{core}_runs", ROOT / "logs" / "bench" / core)


def bug_dirs(core: str, kind: str) -> list[int]:
    corpus = paths(core)[0]
    return sorted(int(p.name) for p in (corpus / kind).iterdir() if p.is_dir() and p.name.isdigit())


def golden_name(core: str, kind: str, d: int) -> str:
    """The golden reference a bug is compared with: its own reference_driver.rtlil (by content) or the mux reference."""
    if kind == "multiplexer":
        return "golden_multiplexer"
    ref = paths(core)[0] / "driver" / str(d) / "reference_driver.rtlil"
    return f"golden_driver_{hashlib.md5(ref.read_bytes()).hexdigest()[:8]}"


def golden_sources(core: str, kinds) -> dict[str, Path]:
    """golden name -> reference netlist to build."""
    corpus = paths(core)[0]
    out = {}
    for k in kinds:
        for d in bug_dirs(core, k):
            n = golden_name(core, k, d)
            if n not in out:
                out[n] = (corpus / "driver" / str(d) / "reference_driver.rtlil") if k == "driver" else None
    return out


def lowered(core: str, src: Path, tag: str) -> Path:
    dst = paths(core)[1] / "lowered" / f"mux_{tag}.rtlil"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(ROOT / "rocket" / "lower_amt.py"), str(src), str(dst)], check=True,
                       capture_output=True)
    return dst


def build(core: str, name: str, rtlil: Path) -> Path:
    out = paths(core)[1] / name
    if not (out / "Vtop_tiny_soc").exists():
        r = subprocess.run(["bash", str(ROOT / "rocket" / "build_dut.sh"), str(rtlil), str(out)],
                           capture_output=True, text=True, env=dict(os.environ, CORE=core))
        if r.returncode or not (out / "Vtop_tiny_soc").exists():
            raise RuntimeError(f"build failed for {core} {name}: {r.stdout[-300:]} {r.stderr[-300:]}")
    return out / "Vtop_tiny_soc"


def build_targets(core: str, kinds) -> list[tuple[str, Path]]:
    corpus = paths(core)[0]
    t = []
    for n, src in golden_sources(core, kinds).items():
        t.append((n, src if src else lowered(core, corpus / "multiplexer" / "reference.rtlil", "reference")))
    for k in kinds:
        for d in bug_dirs(core, k):
            t.append((f"{k}_{d}", corpus / "driver" / str(d) / "host_driver.rtlil" if k == "driver"
                      else lowered(core, corpus / "multiplexer" / str(d) / "host_amt.rtlil", str(d))))
    return t


def migrate_rocket():
    """Before 2026-11-25 Rocket had one driver reference (bug 767's), stored as golden_driver(.json)."""
    corpus, runs, out = paths("rocket")
    n = golden_name("rocket", "driver", 767)
    if (runs / "golden_driver").exists() and not (runs / n).exists():
        (runs / n).symlink_to(runs / "golden_driver")
    for sfx in ("", "_ext"):
        old, new = out / f"golden_driver{sfx}.json", out / f"{n}{sfx}.json"
        if old.exists() and not new.exists():
            new.write_text(old.read_text())


def suffix(profile: str) -> str:
    return "" if profile == "base" else f"_{profile}"


def fuzz(core: str, kind: str, d: int, seed: int, programs: int, profile: str = "base") -> dict:
    _, runs, out = paths(core)
    f = out / f"{kind}_{d}_seed{seed}{suffix(profile)}.json"
    if f.exists():
        return json.loads(f.read_text())
    sim = runs / f"{kind}_{d}" / "Vtop_tiny_soc"
    r = rv64fuzz.campaign(sim, programs, 1000 * seed + d + (0 if profile == "base" else 500000), 40, 400000, True,
                          stop_on_bug=True, profile=profile)
    r.update(core=core, kind=kind, dir=d, seed=seed, budget_programs=programs)
    f.write_text(json.dumps(r))
    return r


def calibrate(core: str, name: str, n: int, profile: str) -> dict:
    _, runs, out = paths(core)
    g = out / f"{name}{suffix(profile)}.json"
    if not g.exists():
        r = rv64fuzz.campaign(runs / name / "Vtop_tiny_soc", n, 777, 40, 400000, True, False, profile=profile)
        r.update(core=core, golden=name)
        g.write_text(json.dumps(r))
    return json.loads(g.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", choices=CORES, default="rocket")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--programs", type=int, default=2000)
    ap.add_argument("--calibrate", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--build-jobs", type=int, default=2)
    ap.add_argument("--only", choices=["driver", "multiplexer"])
    ap.add_argument("--profile", choices=rv64fuzz.PROFILES, default="base")
    ap.add_argument("--build-only", action="store_true")
    a = ap.parse_args()
    core = a.core
    out = paths(core)[2]
    out.mkdir(parents=True, exist_ok=True)
    if core == "rocket":
        migrate_rocket()
    kinds = [a.only] if a.only else ["driver", "multiplexer"]
    # 1. builds (memory: ~3.5 GB each for Rocket, hence few in parallel)
    with ProcessPoolExecutor(a.build_jobs) as ex:
        futs = {ex.submit(build, core, n, src): n for n, src in build_targets(core, kinds)}
        for fu in as_completed(futs):
            try:
                fu.result(); print(f"built {core} {futs[fu]}", flush=True)
            except Exception as e:
                print(f"BUILD FAILED {core} {futs[fu]}: {e}", flush=True)
    if a.build_only:
        return
    # 2. golden calibration: a reference that disagrees with Spike would make every detection meaningless
    goldens = list(golden_sources(core, kinds))
    with ProcessPoolExecutor(min(a.jobs, len(goldens))) as ex:
        futs = {ex.submit(calibrate, core, n, a.calibrate, a.profile): n for n in goldens}
        bad = []
        for fu in as_completed(futs):
            r = fu.result()
            print(f"{futs[fu]} ({a.profile}): {r['programs']} programs, {r['mismatches']} mismatches, "
                  f"{r['nonterminating']} non-terminating", flush=True)
            if r["mismatches"] or r["nonterminating"]:
                bad.append(futs[fu])
    if bad:
        sys.exit(f"golden reference(s) {bad} disagree with Spike: fix the harness before fuzzing bugs")
    # 3. fuzzing
    runs = paths(core)[1]
    jobs = [(k, d, s) for k in kinds for d in bug_dirs(core, k) if (runs / f"{k}_{d}" / "Vtop_tiny_soc").exists()
            for s in range(a.seeds)]
    with ProcessPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(fuzz, core, k, d, s, a.programs, a.profile): (k, d, s) for k, d, s in jobs}
        for fu in as_completed(futs):
            k, d, s = futs[fu]
            r = fu.result()
            fd = r.get("first_detection") or {}
            print(f"{core} {a.profile} {k:11s} {d:5d} seed {s}: " + (
                f"DETECTED at program {fd['n_programs']} ({fd['kind']})" if fd
                else f"not detected in {r['programs']} programs"), flush=True)


if __name__ == "__main__":
    main()
