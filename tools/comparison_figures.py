"""Comparison figures for the paper's state-of-the-art section (called from llm_figures.all_figures).

  fig_cmp_bugs      bugs detected on the same 60 Encarsia bugs: published per-bug results (Encarsia), counts reported by
                    ChipFuzzer on the same corpus, and BenchFuzz's generators (replay-confirmed)
  fig_soundness     per campaign: detections reported vs. confirmed by replay on the bug-free core (incl. archived first
                    versions whose false alarms led to the filters)
  fig_capabilities  capability matrix from docs/capabilities.json (yes / partial / no / not reported)

Palette (validated with the dataviz validator): BenchFuzz random #0072B2, + LLM templates #CC79A7, hand-designed
#009E73, published/reported results neutral gray. Every bar carries a direct label, so identity is never colour alone.
"""
import json
from collections import defaultdict
from pathlib import Path

C_RAND, C_LLM, C_EXT, C_PUB, C_REJ = "#0072B2", "#CC79A7", "#009E73", "#555555", "#D55E00"
INK, MUTED, GRID = "#222222", "#666666", "#dddddd"


def _confirmed_sets(logs: Path, core: str, sfx: str, pub: dict, conf: dict | None = None):
    """Replay-confirmed detected bug numbers {mixups, conditionals} of one profile; (detections, confirmed)."""
    d = logs / "bench" / core
    conf = conf if conf is not None else (json.loads((d / "confirmations.json").read_text())
                                          if (d / "confirmations.json").exists() else {})
    found = {"mixups": set(), "conditionals": set()}
    det = ok = 0
    for f in d.glob(f"*_seed?{sfx}.json"):
        r = json.loads(f.read_text())
        if not r.get("first_detection"):
            continue
        det += 1
        if conf.get(f.name, {}).get("confirmed"):
            ok += 1
            key = "mixups" if r["kind"] == "driver" else "conditionals"
            dirs = pub["driver_dirs"] if key == "mixups" else pub["multiplexer_dirs"]
            found[key].add(dirs.index(r["dir"]) + 1)
    return found, det, ok


def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(GRID); ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0, labelsize=6.5)
    ax.xaxis.grid(True, color=GRID, lw=.5); ax.set_axisbelow(True)


def fig_cmp_bugs(logs: Path, figs: Path, plt, save, root: Path) -> None:
    pubf, litf = root / "docs" / "encarsia_published.json", root / "docs" / "literature_baselines.json"
    if not pubf.exists() or not litf.exists():
        return
    pubs = json.loads(pubf.read_text())
    rep = json.loads(litf.read_text())["systems"]["ChipFuzzer"]["encarsia_bugs_detected"]["per_design_of_30"]
    cores = [("rocket", "Rocket", "RocketCore"), ("boom", "BOOM", "BOOM")]
    # rows top to bottom: (label, kind, color, source)
    rows = [("DifuzzRTL", "pub", "DifuzzRTL"), ("ProcessorFuzz", "pub", "ProcessorFuzz"), ("Cascade", "pub", "Cascade"),
            ("TheHuzz", "rep", "TheHuzz"), ("BMCFuzz", "rep", "BMCFuzz"), ("ChipFuzzer", "rep", "ChipFuzzer (CBG)"),
            ("BenchFuzz random", "ours", ""), ("BenchFuzz + LLM templates", "ours", "_llmflash"),
            ("BenchFuzz hand-designed", "ours", "_ext")]
    color = {"BenchFuzz random": C_RAND, "BenchFuzz + LLM templates": C_LLM, "BenchFuzz hand-designed": C_EXT}
    if not (logs / "bench" / "rocket").exists():
        return
    fig, axes = plt.subplots(1, 2, figsize=(7.1, 2.75), sharey=True)
    for ax, (core, title, rkey) in zip(axes, cores):
        pub = pubs[core]
        ys = list(range(len(rows)))[::-1]
        for y, (label, kind, src) in zip(ys, rows):
            if kind == "pub":
                m, c = len(pub["detected"]["mixups"][src]), len(pub["detected"]["conditionals"][src])
                col, hatch = C_PUB, None
            elif kind == "rep":
                m, c = rep[src][rkey], None
                col, hatch = C_PUB, "////"
            else:
                found, _, _ = _confirmed_sets(logs, core, src, pub)
                m, c = len(found["mixups"]), len(found["conditionals"])
                col, hatch = color[label], None
            if c is None:      # aggregate only (reported by ChipFuzzer): one hatched bar
                ax.barh(y, m, height=.62, color="white", edgecolor=col, hatch=hatch, lw=.6)
                total = m
            else:              # mix-ups solid, conditionals lighter, 2px-equivalent gap
                ax.barh(y, m, height=.62, color=col, lw=0)
                ax.barh(y, c, left=m + .25, height=.62, color=col, alpha=.45, lw=0)
                total = m + c
            ax.text(total + .9, y, f"{total}" + (f" ({m}+{c})" if c is not None else ""), va="center",
                    fontsize=6, color=INK)
        ax.set_yticks(ys); ax.set_yticklabels([r[0] for r in rows], fontsize=6.5, color=INK)
        ax.axhline(ys[5] - .5, color=GRID, lw=.6); ax.axhline(ys[2] - .5, color=GRID, lw=.6)
        ax.set_xlim(0, 30); ax.set_xticks([0, 10, 20, 30])
        ax.set_xlabel("bugs detected (of 30)", fontsize=6.5, color=MUTED)
        ax.set_title(title, fontsize=7.5, color=INK, loc="left")
        _style(ax)
    axes[0].text(-.01, 1.0, "", transform=axes[0].transAxes)
    fig.text(.01, -.04, "solid: signal mix-ups, light: broken conditionals (m+c); hatched: aggregate count reported by "
             "ChipFuzzer on the same corpus (no per-bug data)", fontsize=5.8, color=MUTED)
    fig.tight_layout(pad=.4, w_pad=1.2)
    save(fig, figs, "fig_cmp_bugs"); plt.close(fig)


def fig_soundness(logs: Path, figs: Path, plt, save, root: Path) -> None:
    pubf = root / "docs" / "encarsia_published.json"
    if not pubf.exists() or not (logs / "bench" / "rocket").exists():
        return
    pubs = json.loads(pubf.read_text())
    arch = logs / "archive"
    camps = [("Rocket", "rocket", "", "random", None), ("Rocket", "rocket", "_ext", "hand-designed", None),
             ("Rocket", "rocket", "_ext", "hand-designed, first version", (arch / "rocket_ext_v1", "confirmations_ext.json")),
             ("Rocket", "rocket", "_llm", "LLM (flash-lite)", None), ("Rocket", "rocket", "_llmflash", "LLM (3-flash)", None),
             ("BOOM", "boom", "", "random", None), ("BOOM", "boom", "_ext", "hand-designed", None),
             ("BOOM", "boom", "_llm", "LLM (flash-lite)", None),
             ("BOOM", "boom", "_llm", "LLM (flash-lite), first version", (arch / "boom_llm_v1_no_template_filter", "confirmations_llm.json")),
             ("BOOM", "boom", "_llmflash", "LLM (3-flash)", None)]
    data = []
    for core_t, core, sfx, label, archived in camps:
        if archived:
            adir, cf = archived
            if not (adir / cf).exists():
                continue
            conf = json.loads((adir / cf).read_text())
            det = sum(1 for f in adir.glob(f"*_seed?{sfx}.json") if json.loads(f.read_text()).get("first_detection"))
            ok = sum(1 for v in conf.values() if v.get("confirmed"))
        else:
            _, det, ok = _confirmed_sets(logs, core, sfx, pubs[core])
        if det:
            data.append((f"{core_t}: {label}", det, ok))
    if not data:
        return
    fig, ax = plt.subplots(figsize=(3.45, 2.6))
    ys = list(range(len(data)))[::-1]
    for y, (label, det, ok) in zip(ys, data):
        ax.barh(y, ok, height=.6, color=C_PUB, lw=0)
        if det > ok:
            ax.barh(y, det - ok, left=ok + .6, height=.6, color=C_REJ, lw=0)
        ax.text(det + 2, y, f"{ok}" + (f" + {det - ok} rejected" if det > ok else ""), va="center", fontsize=5.8,
                color=INK)
    ax.set_yticks(ys); ax.set_yticklabels([d[0] for d in data], fontsize=6, color=INK)
    ax.set_xlim(0, max(d[1] for d in data) * 1.45)
    ax.set_xlabel("detections (gray: replay-confirmed, orange: rejected)", fontsize=6, color=MUTED, loc="right")
    _style(ax)
    fig.tight_layout(pad=.3)
    save(fig, figs, "fig_soundness"); plt.close(fig)


def fig_capabilities(logs: Path, figs: Path, plt, save, root: Path) -> None:
    capf = root / "docs" / "capabilities.json"
    if not capf.exists():
        return
    cap = json.loads(capf.read_text())
    systems, rows = cap["systems"], cap["capabilities"]
    glyph = {"yes": "●", "partial": "◐", "no": "○", "nr": "–"}
    shade = {"yes": "#cfe0ef", "partial": "#eaf1f8", "no": "#ffffff", "nr": "#f4f4f4"}
    fig, ax = plt.subplots(figsize=(7.1, 3.0))
    for i, r in enumerate(rows):
        for j, s in enumerate(systems):
            v = r["values"].get(s, "nr")
            ax.add_patch(plt.Rectangle((j - .48, i - .45), .96, .9, color=shade[v], lw=0))
            ax.text(j, i, glyph[v], ha="center", va="center", fontsize=8.5 if v != "nr" else 7,
                    color=INK if v != "nr" else "#999999")
    ax.add_patch(plt.Rectangle((len(systems) - 1.5, -.52), .98, len(rows) + .04, fill=False, ec=C_RAND, lw=1.1,
                               clip_on=False))
    ax.set_xlim(-.5, len(systems) - .42); ax.set_ylim(len(rows) - .45, -.55)
    ax.set_xticks(range(len(systems))); ax.set_xticklabels(systems, rotation=35, ha="right", fontsize=6.5, color=INK)
    ax.set_yticks(range(len(rows))); ax.set_yticklabels([r["name"] for r in rows], fontsize=6.5, color=INK)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    fig.text(.01, -.03, "● yes   ◐ partial   ○ no   – not reported in the cited paper (to our reading)",
             fontsize=6, color=MUTED)
    fig.tight_layout(pad=.3)
    save(fig, figs, "fig_capabilities"); plt.close(fig)


def all_comparison(logs: Path, figs: Path, plt, save, root: Path) -> None:
    fig_cmp_bugs(logs, figs, plt, save, root)
    fig_soundness(logs, figs, plt, save, root)
    fig_capabilities(logs, figs, plt, save, root)
