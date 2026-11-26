"""Dual-hart campaigns (docs/DUAL_HART.md, evaluation plan items 1-3).

  python -m harness.dual_campaign soundness --pairs 200          bug-free system, several memory timings: must be 0 mismatches
  python -m harness.dual_campaign bugs --pairs 300                each L4 bug: pairs until detection, then the replay study
  python -m harness.dual_campaign minimise                         ddmin of each detected failure, free vs forced data order
Results: logs/dual/<command>.json

Replay study, per detected bug: the failing pair is re-run under R other memory timings
  * free:   new timing, arbiter free             -> does the failure reproduce?  (what a fuzzer without replay sees)
  * forced: new timing, recorded grant order     -> does it reproduce?           (this system)
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from harness import dual_cosim as dc
from harness.dual_gen import gen_pair

ROOT = Path(__file__).resolve().parents[1]
DUAL = ROOT / "hw_design/dual"
DELAYS = (0, 1, 3, 6)


def variant(workdir: Path, bug: dict | None) -> Path:
    """Build the system with one bug applied (or clean)."""
    workdir.mkdir(parents=True, exist_ok=True)
    rtl = []
    for name in ("dual_top.v", "arbiter_rr.v", "amo_pcpi.v"):
        text = (DUAL / name).read_text()
        if bug and bug["file"] == name:
            if text.count(bug["find"]) != 1:
                raise ValueError(f"{bug['name']}: pattern found {text.count(bug['find'])} times in {name}")
            text = text.replace(bug["find"], bug["replace"])
        (workdir / name).write_text(text)
        rtl.append(workdir / name)
    return dc.build(workdir / "build", extra_rtl=rtl)


def timing(i: int) -> tuple[int, int]:
    return DELAYS[i % len(DELAYS)], 1000 + i


def soundness(args) -> dict:
    b = variant(args.work / "clean", None)
    rows, t0 = [], time.time()
    for s in range(args.pairs):
        p = gen_pair(s)
        for j in range(len(DELAYS)):
            d, seed = timing(j)
            r = dc.run(b, *p["words"], args.work, args.max_cycles, d, seed)
            v = dc.check(*p["words"], r)
            rows.append({"pair": s, "delay": d, "ok": v["ok"], "end": v["end"], "retired": v["retired"],
                         "grants": v["grants"], "cycles": v["cycles"], "mismatches": v["mismatches"][:1],
                         "order": hash(tuple(g["g"] for g in r["grants"] if not g["i"]))})
    bad = [r for r in rows if not r["ok"]]
    orders = {}
    for r in rows:
        orders.setdefault(r["pair"], set()).add(r.pop("order"))     # order of data grants
    return {"pairs": args.pairs, "runs": len(rows), "mismatching_runs": len(bad), "first_failures": bad[:5],
            "pairs_whose_data_order_depends_on_timing": sum(1 for v in orders.values() if len(v) > 1),
            "mean_retired_per_run": round(sum(sum(r["retired"]) for r in rows) / len(rows), 1),
            "mean_grants_per_run": round(sum(r["grants"] for r in rows) / len(rows), 1),
            "seconds": round(time.time() - t0, 1)}


def bugs(args) -> dict:
    spec = json.loads((DUAL / "l4_bugs.json").read_text())["bugs"]
    if args.only:
        spec = [b for b in spec if b["name"] in args.only.split(",")]
    out = {}
    for bug in spec:
        b = variant(args.work / bug["name"], bug)
        t0, hit = time.time(), None
        for i in range(args.pairs):
            p = gen_pair(i)
            d, seed = timing(i)
            r = dc.run(b, *p["words"], args.work, args.max_cycles, d, seed, tag=bug["name"])
            v = dc.check(*p["words"], r)
            if not v["ok"]:
                hit = {"pair": i, "runs_to_detect": i + 1, "delay": d, "seed": seed, "kind": v["mismatches"][0]["kind"],
                       "first": v["mismatches"][0], "seconds": round(time.time() - t0, 2),
                       "grant_ids": [g["g"] for g in r["grants"] if not g["i"]]}
                break
        row = {"class": bug["class"], "description": bug["description"], "pairs_tried": args.pairs if hit is None else hit["pair"] + 1,
               "detected": hit is not None}
        if hit:
            p = gen_pair(hit["pair"])
            free = forced = 0
            for j in range(args.replays):
                d, seed = DELAYS[(j + 1) % len(DELAYS)], 5000 + j
                free += not dc.check(*p["words"], dc.run(b, *p["words"], args.work, args.max_cycles, d, seed, tag="free"))["ok"]
                forced += not dc.check(*p["words"], dc.run(b, *p["words"], args.work, args.max_cycles, d, seed,
                                                            replay=hit["grant_ids"], tag="forced"))["ok"]
            row.update({k: hit[k] for k in ("runs_to_detect", "delay", "kind", "first", "seconds")},
                       replay={"timings": args.replays, "reproduced_free": free, "reproduced_forced": forced})
        out[bug["name"]] = row
        print(bug["name"], json.dumps({k: row.get(k) for k in ("detected", "runs_to_detect", "kind", "replay")}), flush=True)
    return {"bugs": out, "detected": sum(r["detected"] for r in out.values()), "total": len(out)}


def _ddmin(units: list, fails, budget: int) -> tuple[list, int]:
    """Complement-based delta debugging; `fails(units)` -> bool. Returns (1-minimal-ish units, tests run)."""
    tests, n = 0, 2
    while len(units) >= 2 and tests < budget:
        size = max(1, len(units) // n)
        chunks = [units[i:i + size] for i in range(0, len(units), size)]
        for i in range(len(chunks)):
            cand = [u for j, c in enumerate(chunks) if j != i for u in c]
            tests += 1
            if cand and fails(cand):
                units, n = cand, max(n - 1, 2)
                break
            if tests >= budget:
                break
        else:
            if size == 1:
                break
            n = min(len(units), 2 * n)
    return units, tests


def minimise(args) -> dict:
    from harness.asm import assemble, AsmError
    spec = {b["name"]: b for b in json.loads((DUAL / "l4_bugs.json").read_text())["bugs"]}
    found = json.loads((args.out / "bugs.json").read_text())["bugs"]
    clean = variant(args.work / "clean", None)
    out = {}
    for name, row in found.items():
        if not row["detected"] or (args.only and name not in args.only.split(",")):
            continue
        b = variant(args.work / name, spec[name])
        i = row["runs_to_detect"] - 1
        p = gen_pair(i)
        d, seed = timing(i)
        r0 = dc.run(b, *p["words"], args.work, args.max_cycles, d, seed, tag="m")
        v0 = dc.check(*p["words"], r0)
        kind = v0["mismatches"][0]["kind"]
        sched = [g["g"] for g in r0["grants"] if not g["i"]]
        # units: every source line after the two-line prologue (s0/s1 bases), tagged with its hart
        lines = [s.splitlines() for s in p["src"]]
        units = [(h, k) for h in (0, 1) for k in range(2, len(lines[h]))]

        def words_of(us):
            keep = [set(k for hh, k in us if hh == h) for h in (0, 1)]
            src = [chr(10).join(l for k, l in enumerate(lines[h]) if k < 2 or k in keep[h]) for h in (0, 1)]
            return [assemble(x) for x in src], src

        def make_pred(forced):
            def fails(us):
                try:
                    ws, _ = words_of(us)
                except AsmError:
                    return False
                rr = dc.run(b, *ws, args.work, args.max_cycles, d, seed, replay=sched if forced else None, tag="m")
                vv = dc.check(*ws, rr)
                return (not vv["ok"]) and vv["mismatches"][0]["kind"] == kind
            return fails

        res = {"kind": kind, "units": len(units)}
        for mode in ("free", "forced"):
            t0 = time.time()
            m, tests = _ddmin(units, make_pred(mode == "forced"), args.budget)
            ws, src = words_of(m)
            # robustness of the minimised failure under other timings (same mode)
            rep = sum(not dc.check(*ws, dc.run(b, *ws, args.work, args.max_cycles, DELAYS[(j + 1) % len(DELAYS)], 7000 + j,
                                               replay=sched if mode == "forced" else None, tag="m"))["ok"]
                      for j in range(args.replays))
            clean_ok = dc.check(*ws, dc.run(clean, *ws, args.work, args.max_cycles, d, seed,
                                            replay=sched if mode == "forced" else None, tag="m"))["ok"]
            res[mode] = {"units_left": len(m), "tests": tests, "seconds": round(time.time() - t0, 1),
                         "reproduces": f"{rep}/{args.replays}", "clean_system_passes": clean_ok,
                         "instructions": [len(w) for w in ws], "src": src}
        out[name] = res
        print(name, kind, res["units"], "->", "free", res["free"]["units_left"], res["free"]["reproduces"],
              "| forced", res["forced"]["units_left"], res["forced"]["reproduces"],
              "| clean ok", res["free"]["clean_system_passes"], res["forced"]["clean_system_passes"], flush=True)
    return {"bugs": out}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["soundness", "bugs", "minimise"])
    ap.add_argument("--pairs", type=int, default=100)
    ap.add_argument("--replays", type=int, default=8)
    ap.add_argument("--max-cycles", type=int, default=300_000)
    ap.add_argument("--only", default="")
    ap.add_argument("--budget", type=int, default=300, help="ddmin tests per bug and mode")
    ap.add_argument("--work", type=Path, default=Path("/tmp/benchfuzz_dual"))
    ap.add_argument("--out", type=Path, default=ROOT / "logs/dual")
    args = ap.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    res = {"soundness": soundness, "bugs": bugs, "minimise": minimise}[args.command](args)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{args.command}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "bugs"}, indent=1))


if __name__ == "__main__":
    main()
