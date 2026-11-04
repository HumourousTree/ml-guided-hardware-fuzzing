#!/usr/bin/env python3
"""Aggregate logs/bench/*.json into comparison tables (JSON + LaTeX) and growth curves (PNG).

  python3 tools/analyze_bench.py logs/bench --out logs/bench_report [--thresholds 50 55 60]

Outputs: summary.json, table_coverage.tex, table_bugs.tex, curves_time.png, curves_programs.png.
Compares methods at a COMMON wall-clock horizon and a COMMON program count (the minimum reached by all runs), reports
tests/time-to-threshold, the plateau point (ChipFuzzer rule), the gain after the plateau, unique coverage points and
Mann-Whitney p-values between methods.
"""
import argparse, glob, json, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import benchmark as bm


def at_programs(series, n):
    last = 0.0
    for s in series:
        if s["n_programs"] > n: break
        last = s["pct"]
    return last


def plateau_point(series, window=100, thresh=0.1):
    hist = []
    for s in series:
        hist.append(s["pct"])
        if bm.plateau_reached(hist, window, thresh): return {"n_programs": s["n_programs"], "t": s["t"], "pct": s["pct"]}
    return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("dir"); ap.add_argument("--out", default="logs/bench_report")
    ap.add_argument("--thresholds", nargs="*", type=float, default=[50, 55, 60]); ap.add_argument("--window", type=int, default=100)
    a = ap.parse_args()
    runs = defaultdict(list)
    for f in sorted(glob.glob(f"{a.dir}/*.json")):
        r = json.loads(Path(f).read_text())
        if r.get("series") is not None and not r.get("bug"): runs[r["method"]].append(r)
    bugruns = defaultdict(lambda: defaultdict(list))
    for f in sorted(glob.glob(f"{a.dir}/*.json")):
        r = json.loads(Path(f).read_text())
        if r.get("bug"): bugruns[r["bug"]][r["method"]].append(r)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    summary = {"methods": {}, "pairwise_p": {}, "bugs": {}}
    if runs:
        T = min(r["series"][-1]["t"] for rs in runs.values() for r in rs if r["series"])
        N = min(r["series"][-1]["n_programs"] for rs in runs.values() for r in rs if r["series"])
        summary["common_horizon"] = {"seconds": T, "programs": N}
        final_at_T = {}
        for m, rs in runs.items():
            fT = [bm.coverage_at(r["series"], T) for r in rs]; fN = [at_programs(r["series"], N) for r in rs]
            final_at_T[m] = fT
            d = {"runs": len(rs), "cov_at_T": bm.mean_ci95(fT), "cov_at_N": bm.mean_ci95(fN), "thresholds": {}}
            for th in a.thresholds:
                tt = [bm.tests_to_threshold(r["series"], th) for r in rs]; tm = [bm.time_to_threshold(r["series"], th) for r in rs]
                reached = [x for x in tt if x is not None]
                d["thresholds"][str(th)] = {"reached": f"{len(reached)}/{len(rs)}", "tests_mean": bm.mean_ci95(reached) if reached else None,
                                           "seconds_mean": bm.mean_ci95([x for x in tm if x is not None]) if reached else None}
            pl = [plateau_point(r["series"], a.window) for r in rs]; d["plateau"] = {"reached": f"{sum(p is not None for p in pl)}/{len(rs)}",
                "programs_mean": bm.mean_ci95([p["n_programs"] for p in pl if p]) if any(pl) else None}
            allc = [set(r["covered_points"]) for r in rs]; d["union_points"] = len(set().union(*allc))
            summary["methods"][m] = d
        ms = sorted(runs)
        for i, x in enumerate(ms):
            for y in ms[i + 1:]:
                u, p = bm.mann_whitney_u(final_at_T[x], final_at_T[y]); summary["pairwise_p"][f"{x} vs {y}"] = {"U": u, "p": p, "n": (len(final_at_T[x]), len(final_at_T[y]))}
        unions = {m: set().union(*[set(r["covered_points"]) for r in rs]) for m, rs in runs.items()}
        summary["unique_points"] = {f"{x} not in {y}": len(unions[x] - unions[y]) for x in ms for y in ms if x != y}
        try:
            import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
            for xkey, name, fn in (("t", "time_s", "curves_time.png"), ("n_programs", "programs", "curves_programs.png")):
                plt.figure(figsize=(6, 4))
                for m, rs in runs.items():
                    grid = [i * (T if xkey == "t" else N) / 100 for i in range(1, 101)]
                    ys = [[(bm.coverage_at(r["series"], g) if xkey == "t" else at_programs(r["series"], g)) for g in grid] for r in rs]
                    mean = [sum(c) / len(c) for c in zip(*ys)]; plt.plot(grid, mean, label=f"{m} (n={len(rs)})")
                    if len(rs) > 1:
                        ci = [bm.mean_ci95(list(c))[1] for c in zip(*ys)]; plt.fill_between(grid, [a_ - b for a_, b in zip(mean, ci)], [a_ + b for a_, b in zip(mean, ci)], alpha=.15)
                plt.xlabel(name); plt.ylabel("RTL coverage points (%)"); plt.legend(); plt.grid(alpha=.3); plt.tight_layout(); plt.savefig(out / fn, dpi=160); plt.close()
        except ImportError:
            print("matplotlib not installed: skipping figures")
        rows = ["\\begin{tabular}{l r r r r}\\toprule", "Method & Cov.@T (\\%) & Cov.@N (\\%) & Plateau (progs) & Union pts\\\\\\midrule"]
        for m, d in summary["methods"].items():
            pl = d["plateau"]["programs_mean"]; rows.append(f"{m} & {d['cov_at_T'][0]:.2f} $\\pm$ {d['cov_at_T'][1]:.2f} & {d['cov_at_N'][0]:.2f} $\\pm$ {d['cov_at_N'][1]:.2f} & {('%.0f' % pl[0]) if pl else '--'} & {d['union_points']}\\\\")
        (out / "table_coverage.tex").write_text("\n".join(rows + ["\\bottomrule\\end{tabular}"]))
    if bugruns:
        rows = ["\\begin{tabular}{l l r r r}\\toprule", "Bug & Method & Detected & Programs (mean) & Seconds (mean)\\\\\\midrule"]
        for bug, per in bugruns.items():
            for m, rs in per.items():
                det = [r["bug_detected"] for r in rs if r["bug_detected"]]
                summary["bugs"].setdefault(bug, {})[m] = {"detected": f"{len(det)}/{len(rs)}", "programs": bm.mean_ci95([d['n_programs'] for d in det]) if det else None, "seconds": bm.mean_ci95([d['t'] for d in det]) if det else None}
                rows.append(f"\\texttt{{{bug.replace('_', chr(92) + '_')}}} & {m} & {len(det)}/{len(rs)} & {('%.1f' % summary['bugs'][bug][m]['programs'][0]) if det else '--'} & {('%.1f' % summary['bugs'][bug][m]['seconds'][0]) if det else '--'}\\\\")
        (out / "table_bugs.tex").write_text("\n".join(rows + ["\\bottomrule\\end{tabular}"]))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str)); print(json.dumps(summary, indent=1, default=str)[:3000])


if __name__ == "__main__":
    main()
