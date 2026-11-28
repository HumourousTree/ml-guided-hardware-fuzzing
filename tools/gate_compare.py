"""Per-bug comparison (replay-confirmed detections only) of gate-mode vs filter-mode BOOM campaigns.

  python tools/gate_compare.py . results/v0.9/gate/boom/<profile> <profile>
"""
import glob, json, os, re, sys, collections
repo, gdir, prof = sys.argv[1:4]
suf = {"base": "", "ext": "_ext", "llmflash": "_llmflash", "llmtgt": "_llmtgt", "llmctl": "_llmctl"}[prof]
filt = os.path.join(repo, "results/v0.4/bench/boom")
pub = json.load(open(os.path.join(repo, "docs/encarsia_published.json")))["boom"]
bugs = [("driver", d) for d in sorted(pub["driver_dirs"])] + [("multiplexer", d) for d in sorted(pub["multiplexer_dirs"])]
CONF = json.load(open(os.path.join(filt, "confirmations.json")))
CONF.update(json.load(open(os.path.join(gdir, "confirmations.json"))))
def stats(path_fn):
    det, firsts, progs, refdis = 0, [], 0, 0
    n = 0
    for s in range(5):
        p = path_fn(s)
        if not os.path.exists(p):
            continue
        r = json.load(open(p)); n += 1
        progs += r["programs"]; refdis += r.get("reference_disagreements", 0)
        fd = r.get("first_detection")
        if fd and not CONF.get(os.path.basename(p), {}).get("confirmed", True):
            fd = None                                   # rejected by replay on the bug-free core (not a detection)
        if fd:
            det += 1; firsts.append(fd["n_programs"])
    return n, det, firsts, progs, refdis
tot = collections.Counter()
print(f"{'bug':<18}{'filter det/seeds':>17}{'gate det/seeds':>16}  gate first-detections   ref-disagree/programs")
for k, d in bugs:
    fn, fdt, ff, fp, _ = stats(lambda s: f"{filt}/{k}_{d}_seed{s}{suf}.json")
    gn, gdt, gf, gp, gr = stats(lambda s: f"{gdir}/{k}_{d}_seed{s}{suf}_gate.json")
    tot["filter_bugs"] += fdt > 0; tot["gate_bugs"] += gdt > 0
    tot["filter_seed_det"] += fdt; tot["gate_seed_det"] += gdt
    flag = " <-- gate only" if gdt and not fdt else (" <-- filter only" if fdt and not gdt else "")
    print(f"{k[:3]} {d:<14}{f'{fdt}/{fn}':>17}{f'{gdt}/{gn}':>16}  {str(sorted(gf)):<22} {gr}/{gp}{flag}")
print(dict(tot))
cal = glob.glob(f"{gdir}/golden_*_gate.json")
mm = sum(json.load(open(p)).get("mismatches", 0) for p in cal); pr = sum(json.load(open(p))["programs"] for p in cal)
print(f"calibration (bug-free cores, gate mode): {mm} mismatches in {pr} programs")
