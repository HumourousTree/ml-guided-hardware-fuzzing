"""Figures for the LLM and oracle experiments (A1-A5, B1), drawn from the same logs as compare_literature.py.

Called from compare_literature.main(); each function returns silently when its data is missing.
  fig_llm_repair     per-bug certified pass@1: informed vs blind (vs stronger model), template result, legacy/plain split
  fig_llm_campaign   A3: failing bug programs per iteration while the closed loop repairs five bugs at once
  fig_oracle         B1: programs to first detection, lockstep vs end-state oracle, per bug
  fig_llm_hybrid     A4: coverage after the plateau, LLM target-line generator vs mutation and mutation-then-random
"""
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

# one palette for every LLM figure; colour-blind safe (Okabe-Ito)
C = {"informed": "#0072B2", "blind": "#E69F00", "strong": "#009E73", "template": "#555555",
     "lockstep": "#0072B2", "endstate": "#D55E00", "llm": "#CC79A7", "mutation": "#DD8452", "hybrid-rand": "#55A868"}


def _style(ax, grid="x"):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(alpha=.3, lw=.4, axis=grid)
    ax.tick_params(length=2, width=.5)


def fig_llm_repair(logs: Path, figs: Path, plt, save, root: Path) -> None:
    exp = logs / "experiments"
    model_file = exp / "llm_track_model.txt"
    if not model_file.exists():
        return
    main = model_file.read_text().strip()
    load = lambda p: json.loads(p.read_text()) if p.exists() else {}
    inf, bl = load(exp / f"llm_repair_informed_{main}.json"), load(exp / f"llm_repair_blind_{main}.json")
    strong = {f.stem[len("llm_repair_informed_"):]: load(f) for f in exp.glob("llm_repair_informed_*.json")
              if f.stem != f"llm_repair_informed_{main}"}
    tpl = load(exp / "repair_baselines.json")
    legacy = set(json.loads((root / "hw_design" / "bug_manifest.json").read_text()))
    bugs = [b for b, r in inf.items() if r.get("complete")]
    if not bugs:
        return
    # legacy bugs first, then plain; within a group by informed pass@1 (best on top)
    bugs.sort(key=lambda b: (b not in legacy, -inf[b]["pass@1_certified"], b))
    fig, ax = plt.subplots(figsize=(3.45, 0.2 * len(bugs) + 0.9))
    ys = list(range(len(bugs)))[::-1]
    for y, b in zip(ys, bugs):
        xi = inf[b]["pass@1_certified"]
        xb = (bl.get(b) or {}).get("pass@1_certified")
        if xb is not None:
            ax.plot([xb, xi], [y, y], color="#bbbbbb", lw=.8, zorder=1)
            ax.scatter([xb], [y], s=16, facecolor="white", edgecolor=C["blind"], lw=1, zorder=3,
                       label="blind control" if y == ys[0] else None)
        ax.scatter([xi], [y], s=16, color=C["informed"], zorder=4, label=f"with failure data" if y == ys[0] else None)
        for i, (m, recs) in enumerate(strong.items()):
            r = recs.get(b) or {}
            if r.get("complete"):
                ax.scatter([r["pass@1_certified"]], [y + .22], s=12, marker="D", color=C["strong"], zorder=5,
                           label=m if y == ys[0] else None)
        fb = (inf[b].get("feedback_run") or {}).get("certified")
        t = tpl.get(f"{b}|template") or {}
        tok = t.get("accepted") and (t.get("formal") or {}).get("verdict") in ("identical", "proven")
        ax.text(1.12, y, "+" if fb else "-", ha="center", va="center", fontsize=6.5, color=C["informed"] if fb else "#aaaaaa")
        ax.text(1.27, y, "+" if tok else "-", ha="center", va="center", fontsize=6.5, color=C["template"] if tok else "#aaaaaa")
    ax.text(1.12, len(bugs) - .35, "loop", ha="center", fontsize=5.3, color="#444444")
    ax.text(1.27, len(bugs) - .35, "tmpl", ha="center", fontsize=5.3, color="#444444")
    ax.axvline(1.05, color="#dddddd", lw=.5)
    n_leg = sum(b in legacy for b in bugs)
    if 0 < n_leg < len(bugs):
        ax.axhline(len(bugs) - n_leg - .5, color="#999999", lw=.5, ls="--")      # legacy above, plain below (caption)
    ax.set_yticks(ys); ax.set_yticklabels(bugs, fontsize=5.3, family="monospace")
    ax.set_xlim(-0.05, 1.34); ax.set_xticks([0, .25, .5, .75, 1]); ax.set_ylim(-.7, len(bugs) - .1)
    ax.set_xlabel("certified pass@1")
    _style(ax)
    ax.legend(fontsize=5.5, loc="lower center", bbox_to_anchor=(0.42, 1.0), ncol=3, frameon=False, handletextpad=.3, columnspacing=.8)
    fig.tight_layout(pad=.3)
    save(fig, figs, "fig_llm_repair"); plt.close(fig)


def fig_llm_campaign(logs: Path, figs: Path, plt, save) -> None:
    p = logs / "experiments" / "llm_campaign" / "telemetry.json"
    if not p.exists():
        return
    t = json.loads(p.read_text())
    its = t.get("iterations") or []
    if not its:
        return
    nb = len(its[0].get("active_bugs") or [])
    last_rep = max((it["iteration"] for it in its if "Repair" in str(it.get("mode"))), default=10)
    show = [it for it in its if it["iteration"] <= max(last_rep + 4, 12)]
    xs = [it["iteration"] for it in show]
    fail = [len(it.get("bug_programs_failing") or []) if it.get("bug_programs_failing") is not None else (0 if it.get("comparison_ok") else None)
            for it in show]
    # carry the last known count through iterations that do not record it
    cur = nb
    ys = []
    for it, f in zip(show, fail):
        if "Repair" in str(it.get("mode")) and f is not None:
            cur = f
        elif it.get("comparison_ok"):
            cur = 0 if it["iteration"] > last_rep else cur
        ys.append(cur)
    fig, ax = plt.subplots(figsize=(3.45, 1.7))
    ax.step(xs, ys, where="post", color=C["informed"], lw=1.2)
    marks = {"accepted": ("o", C["informed"]), "rolled back": ("x", C["endstate"]), "all rejected": ("v", "#999999")}
    seen = set()
    for it, y in zip(show, ys):
        if "Repair" not in str(it.get("mode")):
            continue
        src = str(it.get("llm_source"))
        kind = "rolled back" if "rolled_back" in src else ("all rejected" if src == "exhausted" else "accepted")
        m, c = marks[kind]
        ax.scatter([it["iteration"]], [y], marker=m, s=14, color=c, lw=1, zorder=4, label=kind if kind not in seen else None)
        seen.add(kind)
    ax.set_xlabel("campaign iteration"); ax.set_ylabel("bug programs failing")
    ax.set_yticks(range(0, nb + 1)); ax.set_ylim(-.3, nb + .8)
    ax.text(xs[-1], .35, f"iterations {xs[-1] + 1}-{len(its)}: all match", ha="right", fontsize=5.5, color="#444444")
    _style(ax, grid="y")
    ax.legend(fontsize=5.5, frameon=False, loc="upper right", title="repair attempt", title_fontsize=5.5)
    fig.tight_layout(pad=.3)
    save(fig, figs, "fig_llm_campaign"); plt.close(fig)


def fig_oracle(logs: Path, figs: Path, plt, save) -> None:
    d = logs / "bench" / "oracle"
    if not d.exists():
        return
    per = defaultdict(list)
    for f in sorted(d.glob("*_bug-*_seed*.json")):
        r = json.loads(f.read_text())
        if r.get("bug_detected") and r.get("bug_detected_end_state"):
            per[r["bug"]].append((r["bug_detected"]["n_programs"], r["bug_detected_end_state"]["n_programs"]))
    if not per:
        return
    fig, ax = plt.subplots(figsize=(3.45, 2.3))
    pts = []
    for b, xs in sorted(per.items()):
        ls, es = st.median(x for x, _ in xs), st.median(y for _, y in xs)
        pts.append((ls, es, b))
    hi = max(max(p[0], p[1]) for p in pts) * 2
    ax.plot([0.7, hi], [0.7, hi], color="#999999", lw=.6, ls="--", zorder=1)
    groups = defaultdict(list)
    for ls, es, b in pts:
        groups[(ls, es)].append(b)
    for (ls, es), bs in groups.items():
        later = es > ls
        ax.scatter([ls], [es], s=14 + 10 * len(bs), color=C["endstate"] if later else C["lockstep"], zorder=3, lw=0, alpha=.9)
        if len(bs) > 1:
            ax.annotate(str(len(bs)), (ls, es), fontsize=4.5, ha="center", va="center", color="white", zorder=4)
        if later:
            ax.annotate(", ".join(bs), (ls, es), fontsize=4.8, xytext=(3, 3), textcoords="offset points", color="#333333",
                        va="bottom")
    ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlim(0.7, hi); ax.set_ylim(0.7, hi)
    ax.set_xlabel("lockstep: median programs to detection"); ax.set_ylabel("end-state: median programs")
    ax.text(0.8, hi / 1.6, "above the diagonal: end-state oracle detects later", fontsize=5.5, color=C["endstate"])
    ax.text(hi / 1.3, 0.85, f"{len(pts)} bugs, median over paired runs\nnumber in dot = bugs with equal medians",
            fontsize=4.8, color="#555555", ha="right", va="bottom")
    _style(ax, grid="both")
    fig.tight_layout(pad=.3)
    save(fig, figs, "fig_oracle"); plt.close(fig)


def fig_llm_hybrid(logs: Path, figs: Path, plt, save, at_programs) -> None:
    d = logs / "bench" / "llm_hybrid"
    runs = [json.loads(f.read_text()) for f in sorted(d.glob("hybrid_seed*.json"))] if d.exists() else []
    runs = [r for r in runs if not r.get("llm_starved")]
    if not runs:
        return
    n = min(r["series"][-1]["n_programs"] for r in runs)
    grid = list(range(1, n + 1))
    fig, ax = plt.subplots(figsize=(3.45, 1.9))

    def curve(series_list, color, label, ls="-"):
        ys = [st.mean(at_programs(s, x) for s in series_list) for x in grid]
        ax.plot(grid, ys, color=color, lw=1.1, ls=ls, label=label)
    curve([r["series"] for r in runs], C["llm"], f"mutation, then LLM targets ({len(runs)} seed{'s' if len(runs) > 1 else ''})")
    for m, lab, ls in (("mutation", "mutation", "-"), ("hybrid-rand", "mutation, then random", "--")):
        refs = [logs / "bench" / "coverage" / f"{m}_seed{r['seed']}.json" for r in runs]
        if all(p.exists() for p in refs):
            curve([json.loads(p.read_text())["series"] for p in refs], C[m], lab, ls)
    sem = [(p["n_programs"], p["pct"]) for r in runs[:1] for p in r["series"] if p.get("src") == "semantic"]
    if sem:
        ax.scatter([x for x, _ in sem], [y for _, y in sem], s=4, color=C["llm"], alpha=.6, lw=0, zorder=4)
    lo = min(at_programs(r["series"], max(1, int(n * .3))) for r in runs) - 1
    ax.set_xlim(int(n * .3), n); ax.set_ylim(lo, None)
    sws = [(r.get("switched_at") or {}).get("programs") for r in runs if (r.get("switched_at") or {}).get("programs")]
    if sws:
        ax.axvline(st.median(sws), color="#999999", lw=.6, ls=":")
        ax.text(st.median(sws) + 3, ax.get_ylim()[1] - .1, "plateau rule fires:\nswitch generator", fontsize=5.3,
                color="#444444", va="top")
    ax.set_xlabel("programs"); ax.set_ylabel("RTL coverage (%)")
    _style(ax, grid="y")
    ax.legend(fontsize=5.5, frameon=False, loc="lower right")
    fig.tight_layout(pad=.3)
    save(fig, figs, "fig_llm_hybrid"); plt.close(fig)


def all_figures(logs: Path, figs: Path, plt, save, root: Path, at_programs) -> None:
    if plt is None:
        return

    def save(fig, figs, name):                 # tight bounding box: axis labels and legends are never clipped
        fig.savefig(figs / f"{name}.pdf", bbox_inches="tight", pad_inches=.02)
        fig.savefig(figs / f"{name}.png", dpi=200, bbox_inches="tight", pad_inches=.02)
    fig_llm_repair(logs, figs, plt, save, root)
    fig_llm_campaign(logs, figs, plt, save)
    fig_oracle(logs, figs, plt, save)
    fig_llm_hybrid(logs, figs, plt, save, at_programs)
