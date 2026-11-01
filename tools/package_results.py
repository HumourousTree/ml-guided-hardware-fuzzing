#!/usr/bin/env python3
"""Copy the evaluation results into a compact, committable folder.

  python tools/package_results.py [--logs logs] [--out results/v0.4]

Benchmark runs keep every field except the per-point coverage key list (replaced by per-page counts) and the series is
thinned to log-spaced points plus every point where coverage changed; experiment JSON, logs and the generated
comparison report are copied unchanged.  `tools/compare_literature.py --logs results/v0.4` regenerates the paper from it
except the unique-point analysis, which needs the raw covered_points.
"""
import argparse
import difflib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def compact_run(r: dict) -> dict:
    from compare_literature import plateau_programs   # computed on the full series before thinning
    r = dict(r, plateau_programs=plateau_programs(r))
    s = r.get("series") or []
    keep, last = [], None
    marks = {int(round(1.15 ** k)) for k in range(80)}
    for i, p in enumerate(s):
        if i == len(s) - 1 or p["n_programs"] in marks or p["pct"] != last:
            keep.append(p)
        last = p["pct"]
    out = dict(r, series=keep)
    pts = out.pop("covered_points", None)
    if pts is not None:
        pages = {}
        for k in pts:
            pg = next((x for x in ("v_line", "v_toggle", "v_branch") if x in k), "other")
            pages[pg] = pages.get(pg, 0) + 1
        out["covered_pages"] = pages
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=str(ROOT / "logs"))
    ap.add_argument("--out", default=str(ROOT / "results" / "v0.4"))
    a = ap.parse_args()
    logs, out = Path(a.logs), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in sorted((logs / "bench").iterdir()):
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*_seed*.json")):
            dst = out / "bench" / f.relative_to(logs / "bench")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(json.dumps(compact_run(json.loads(f.read_text()))))
            n += 1
    exp = out / "experiments"
    exp.mkdir(exist_ok=True)
    for f in (logs / "experiments").iterdir():
        if f.is_file() and f.suffix in (".json", ".log", ".dat"):
            shutil.copy2(f, exp / f.name)
    cands = logs / "experiments" / "repair_candidates"
    if cands.exists():                       # accepted patches as unified diffs against the golden RTL (not full RTL copies)
        golden = (ROOT / "hw_design" / "picorv32_golden.sv").read_text().splitlines(keepends=True)
        (exp / "repair_patches").mkdir(exist_ok=True)
        for f in sorted(cands.glob("*.sv")):
            diff = difflib.unified_diff(golden, f.read_text().splitlines(keepends=True), "golden/picorv32.v", f"patched/{f.stem}")
            (exp / "repair_patches" / f"{f.stem}.diff").write_text("".join(diff))
    for f in ("comparison_report.md",):
        if (logs / f).exists():
            shutil.copy2(logs / f, out / f)
    arch = logs / "archive" / "runC_pico_campaign_telemetry.json"
    if arch.exists():
        t = json.loads(arch.read_text())
        t["iterations"] = [{k: it.get(k) for k in ("iteration", "mode", "match_rate_pct", "avg_cpi", "unified_coverage_pct",
                                                   "llm_called", "llm_source", "rtl_improved", "active_bugs", "mismatch_total")}
                           for it in t.get("iterations", [])]
        (out / "archive").mkdir(exist_ok=True)
        (out / "archive" / "runC_pico_campaign_telemetry.json").write_text(json.dumps(t))
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"{n} benchmark runs, {size / 1e6:.1f} MB -> {out}")


if __name__ == "__main__":
    main()
