#!/usr/bin/env python3
"""List the RTL lines / toggle signals that no run has covered, from Verilator coverage .dat files.

  python3 tools/uncovered_lines.py /tmp/benchfuzz-picorv32-*/*.coverage.dat --rtl hw_design/picorv32_golden.sv [--top 40]

The .dat files are overwritten every iteration, so run this right after (or during) a campaign; the merge below
unions whatever files are present.  Output: unreached line ranges with source text, grouped so that dead
configuration code (IRQ, compressed ISA, counters, trace...) is easy to tell apart from reachable gaps, and the
toggle signals with the most never-toggled bits.
"""
import argparse, glob, re, sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(r"^C '(.*)' (\d+)\s*$")


def parse(path):
    pts = {}
    for raw in Path(path).read_text(errors="ignore").splitlines():
        m = LINE.match(raw)
        if not m: continue
        kv = {}
        for item in m.group(1).split("\x01"):
            if "\x02" in item:
                k, v = item.split("\x02", 1); kv[k] = v
        pts[m.group(1)] = (kv, int(m.group(2)))
    return pts


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("dats", nargs="+"); ap.add_argument("--rtl"); ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--universe-dat", help="with benchmark run .json inputs: any .dat of the same build (supplies every point key)")
    ap.add_argument("--json-out", help="write the unreached line ranges and signals as JSON")
    a = ap.parse_args()
    files = [f for pat in a.dats for f in glob.glob(pat)]
    if not files: sys.exit("no .dat files found")
    merged = {}
    if files[0].endswith(".json"):          # benchmark runs: covered_points are the hit keys, the universe .dat the rest
        import json
        if not a.universe_dat: sys.exit("--universe-dat is required with .json inputs")
        hit = set().union(*(set(json.loads(Path(f).read_text())["covered_points"]) for f in files))
        merged = {k: (kv, int(k in hit)) for k, (kv, _) in parse(a.universe_dat).items()}
        files = files + [a.universe_dat]
    for f in ([] if merged else files):
        for key, (kv, cnt) in parse(f).items():
            if key in merged: merged[key] = (merged[key][0], merged[key][1] + cnt)
            else: merged[key] = (kv, cnt)
    src = Path(a.rtl).read_text().splitlines() if a.rtl else []
    lines = defaultdict(int); toggles = defaultdict(lambda: [0, 0])
    for kv, cnt in merged.values():
        page = kv.get("page", "").split("/")[0]
        if page == "v_line":
            lines[int(kv.get("l", 0))] += cnt
        elif page == "v_toggle":
            t = toggles[kv.get("o", "?")]; t[1] += 1; t[0] += cnt > 0
    miss = sorted(l for l, c in lines.items() if c == 0)
    print(f"{len(files)} files merged; line points {len(lines)}, unreached {len(miss)} ({100 * len(miss) / max(1, len(lines)):.1f}%)")
    ranges, start = [], None
    for i, l in enumerate(miss):
        if start is None: start = prev = l
        elif l - prev <= 2: prev = l
        else: ranges.append((start, prev)); start = prev = l
    if start is not None: ranges.append((start, prev))
    print(f"\n{len(ranges)} unreached regions (largest first):")
    for s, e in sorted(ranges, key=lambda r: r[0] - r[1])[:a.top]:
        text = src[s - 1].strip()[:90] if src and s - 1 < len(src) else ""
        print(f"  lines {s}-{e} ({e - s + 1:3d})  {text}")
    bad = sorted(((n, t) for n, t in toggles.items() if t[0] < t[1]), key=lambda x: x[1][0] - x[1][1])[:a.top]
    print(f"\nsignals with never-toggled bits (top {len(bad)}):")
    for n, (hit, tot) in bad: print(f"  {n:40s} {hit}/{tot} bits toggled")
    if a.json_out:
        import json
        tog_tot = sum(t[1] for t in toggles.values()); tog_hit = sum(t[0] for t in toggles.values())
        Path(a.json_out).write_text(json.dumps({
            "line_points": len(lines), "line_unreached": len(miss), "toggle_bits": tog_tot, "toggle_unreached": tog_tot - tog_hit,
            "regions": [{"start": s, "end": e, "text": src[s - 1].strip()[:120] if src and s - 1 < len(src) else ""}
                        for s, e in sorted(ranges, key=lambda r: r[0] - r[1])],
            "signals": [{"signal": n, "toggled": h, "bits": t} for n, (h, t) in
                        sorted(toggles.items(), key=lambda x: x[1][0] - x[1][1]) if h < t]}, indent=1))


if __name__ == "__main__":
    main()
