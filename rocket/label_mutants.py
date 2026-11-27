#!/usr/bin/env python3
"""Track 1, step 2: label training mutants with a fixed program pool -> kill matrix (training data for the
bug-trained generator).

  python3 rocket/label_mutants.py pool  --core rocket [--profiles base:120,ext:120,llmflash:120] [--seed 7] --out pool.json
  python3 rocket/label_mutants.py label --core rocket --kind driver --manifest M.json --pool pool.json
                                        --shard K --of N [--jobs 4] --out labels_K.jsonl

pool:  generates the programs (rocket/rv64fuzz.py profiles, core quirks applied), runs each on Spike and on the bug-free
       reference core of EnCorpus (lowered multiplexer reference) and keeps only programs on which the two agree and
       terminate, so a label never comes from a core/reference difference. Stores each program's assembly and Spike dump.
label: for its share of the manifest's mutants (item i -> shard i mod N), regenerates the mutant netlists with the
       mutant factory (deterministic; the host netlist hash must match the manifest), builds each simulator, runs every
       pool program and writes one JSONL row per (mutant, program): killed, how (mismatch / hang), first differing dump.
"""
import argparse
import collections
import json
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rocket"))
import mutant_factory  # noqa: E402
import rv64fuzz  # noqa: E402

SIMLEN = 100_000          # programs retire a few hundred instructions; a correct core stops far earlier


def _gen(core: str, profile: str, rng: random.Random) -> str:
    q = rv64fuzz.QUIRKS.get(core, {})
    tpls = None
    if profile in rv64fuzz.TEMPLATE_FILES:
        tf = ROOT / "rocket" / "templates" / rv64fuzz.TEMPLATE_FILES[profile]
        tpls = json.loads(tf.read_text())["templates"]
    return rv64fuzz.gen_program(rng, 40, True, profile, q.get("subst"), tpls, q.get("keep_frm_valid", False),
                                q.get("template_subst"))


def _build(src: Path, out: Path, core: str, lower: bool) -> Path:
    if lower:
        low = out.parent / f"{out.name}.lowered.rtlil"
        subprocess.run([sys.executable, str(ROOT / "rocket" / "lower_amt.py"), str(src), str(low)], check=True,
                       capture_output=True)
        src = low
    r = subprocess.run(["bash", str(ROOT / "rocket" / "build_dut.sh"), str(src), str(out)], capture_output=True,
                       text=True, env={**__import__("os").environ, "CORE": core})
    if r.returncode or not (out / "Vtop_tiny_soc").exists():
        raise RuntimeError(f"build failed: {r.stdout[-300:]} {r.stderr[-300:]}")
    return out / "Vtop_tiny_soc"


def _run_one(args):
    sim, asm, expected = args
    with tempfile.TemporaryDirectory() as w:
        elf = rv64fuzz.build_elf(asm, Path(w))
        got, stopped = rv64fuzz.run_rocket(Path(sim), elf, SIMLEN)
    if not stopped:
        return True, "hang", None
    if got != expected:
        first = next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), min(len(got), len(expected)))
        return True, "mismatch", first
    return False, None, None


def _gate(ref: Path, asm: str):
    """The pool gate: returns (Spike's dump, None) if Spike and the bug-free reference agree and both stop, else
    (None, reason)."""
    with tempfile.TemporaryDirectory() as w:
        try:
            elf = rv64fuzz.build_elf(asm, Path(w))
        except Exception:
            return None, "assembly"
        try:
            exp = rv64fuzz.run_spike(elf)
        except Exception:
            exp = None
        if not exp:
            return None, "spike"
        got, stopped = rv64fuzz.run_rocket(ref, elf, SIMLEN)
    if not stopped:
        return None, "reference_hang"
    if got != exp:
        return None, "reference_disagrees"
    return exp, None


def cmd_pool(a):
    """Generated pool (--profiles) or a pool of learned-generator samples (--from-samples, rocket/genmodel/sample.py
    outputs; the file stem becomes the profile, e.g. samples_base_dpo_rocket_ext). Both pass the same gate."""
    work = Path(tempfile.mkdtemp(prefix="pool-"))
    ref = _build(mutant_factory.CORPUS / a.core / "multiplexer" / "reference.rtlil", work / "ref", a.core, True)
    progs, dropped, gate = [], 0, {}
    if a.from_samples:
        for f in a.from_samples:
            src = Path(f).stem
            st = gate.setdefault(src, collections.Counter())
            for smp in json.loads(Path(f).read_text())["samples"]:
                if a.per_file and st["kept"] >= a.per_file:     # same pool size as a generator profile
                    break
                st["samples"] += 1
                if not smp.get("asm"):
                    st["static_or_decode"] += 1; continue
                exp, why = _gate(ref, smp["asm"])
                if why:
                    st[why] += 1; dropped += 1; continue
                st["kept"] += 1
                progs.append({"id": f"{src}-{smp['i']}", "profile": src, "asm": smp["asm"], "expected": exp})
    else:
        for spec in a.profiles.split(","):
            prof, n = spec.split(":")
            rng = random.Random(f"{a.seed}-{a.core}-{prof}")
            for i in range(int(n)):
                asm = _gen(a.core, prof, rng)
                exp, why = _gate(ref, asm)
                if why:
                    dropped += 1; continue
                progs.append({"id": f"{prof}-{i}", "profile": prof, "asm": asm, "expected": exp})
    Path(a.out).write_text(json.dumps({"core": a.core, "seed": a.seed, "profiles": a.profiles, "simlen": SIMLEN,
                                       "dropped_reference_disagreement": dropped,
                                       "gate": {k: dict(v) for k, v in gate.items()}, "programs": progs}))
    print(f"pool {a.core}: {len(progs)} programs kept, {dropped} dropped (assembly or Spike failure, or the bug-free "
          f"core disagrees with Spike / does not stop)")
    for k, v in gate.items():
        print(f"  {k}: {dict(v)}")


def target_class(core: str, kind: str, d, published: bool) -> str:
    """Location class of a mutant, as rocket/genmodel/data.py names it (ctl:mod:<class>, "other" outside the
    classes of rocket/genmodel/modules.json); read directly so this script needs no numpy / torch."""
    import re as _re
    if published:
        mods = json.loads((ROOT / "data/mutants/published_locations.json").read_text())[core][kind][str(d)]["modules"]
    else:
        mods = json.loads((ROOT / f"data/mutants/{core}_{kind}_locations.json").read_text())["locations"][str(d)]["modules"]
    classes = json.loads((ROOT / "rocket/genmodel/modules.json").read_text())["classes"]
    c = _re.sub(r"_\d+$", "", mods[0]) if mods else "other"
    return c if c in classes else "other"


def cmd_label(a):
    import gzip
    pool = json.loads(gzip.open(a.pool, "rt").read() if a.pool.endswith(".gz") else Path(a.pool).read_text())
    if a.limit_programs:
        pool["programs"] = pool["programs"][:a.limit_programs]
    if a.published:   # the published Encarsia bugs (held out from training), straight from EnCorpus
        pub = json.loads((ROOT / "docs" / "encarsia_published.json").read_text())[a.core]
        dirs = pub["driver_dirs"] if a.kind == "driver" else pub["multiplexer_dirs"]
        man = {"core": a.core, "kind": a.kind, "mutants": [{"dir": d, "split": "published"} for d in dirs]}
        a.mutants = str(mutant_factory.CORPUS.parent)
    else:
        man = json.loads(Path(a.manifest).read_text())
    a.yosys = a.yosys or mutant_factory.INJECTOR[a.kind]
    assert man["core"] == pool["core"] == a.core and man["kind"] == a.kind
    sel = [m for m in man["mutants"] if a.split == "all" or m["split"] == a.split]
    if a.only_unkilled:     # on-policy rounds: only mutants that no program in these label files kills
        hit = set()
        for lp in a.only_unkilled:
            for l in (gzip.open(lp, "rt") if lp.endswith(".gz") else open(lp)):
                r = json.loads(l)
                if r.get("killed") and r["core"] == a.core and r["kind"] == a.kind:
                    hit.add(str(r["mutant"]))
        sel = [m for m in sel if str(m["dir"]) not in hit]
        print(f"{len(sel)} mutants not killed by {len(a.only_unkilled)} label file(s)", flush=True)
    if a.only_mutants:      # named mutants only (filling in shards that hit the job time limit)
        want = {s.strip() for s in a.only_mutants.split(",") if s.strip()}
        sel = [m for m in sel if str(m["dir"]) in want]
    if a.prog_of > 1:       # and only a slice of the pool, so a slow mutant can be spread over several runners
        pool["programs"] = pool["programs"][a.prog_shard::a.prog_of]
    if a.max_mutants and len(sel) > a.max_mutants:   # a fixed random subset, the same on every shard
        sel = sorted(random.Random(f"{a.core}-{a.kind}-subset").sample(sel, a.max_mutants), key=lambda m: m["dir"])
    mine = [m for i, m in enumerate(sel) if i % a.of == a.shard]
    mdir = (mutant_factory.CORPUS if a.published else Path(a.mutants)) / a.core / a.kind
    if not a.published and not any(mdir.glob("*/" + mutant_factory.HOST_FILE[a.kind])):
        mutant_factory.inject(a.yosys, a.core, a.kind, man["num_bugs_requested"], mdir)
    out = open(a.out, "w")
    work = Path(tempfile.mkdtemp(prefix="label-"))
    for m in mine:
        host = mdir / str(m["dir"]) / mutant_factory.HOST_FILE[a.kind]
        if not a.published and mutant_factory.sha(host) != m["sha256"]:
            raise SystemExit(f"mutant {m['dir']}: regenerated netlist differs from the manifest (injection not deterministic)")
        try:
            sim = _build(host, work / f"m{m['dir']}", a.core, a.kind == "multiplexer")
        except Exception as e:
            out.write(json.dumps({"core": a.core, "kind": a.kind, "mutant": m["dir"], "split": m["split"],
                                  "error": str(e)[:300]}) + "\n"); out.flush()
            continue
        progs = pool["programs"]
        if a.match_class:   # bug-targeted evaluation: only the programs sampled for this mutant's location class
            cls = target_class(a.core, a.kind, m["dir"], a.published)
            progs = [p for p in progs if f"@{cls}_" in p["profile"]]
            print(f"mutant {a.kind} {m['dir']}: class {cls}, {len(progs)} targeted programs", flush=True)
            if not progs:
                subprocess.run(["rm", "-rf", str(work / f"m{m['dir']}")]); continue
        jobs = [(str(sim), p["asm"], p["expected"]) for p in progs]
        with ProcessPoolExecutor(a.jobs) as ex:
            res = list(ex.map(_run_one, jobs))
        for p, (killed, how, first) in zip(progs, res):
            out.write(json.dumps({"core": a.core, "kind": a.kind, "mutant": m["dir"], "split": m["split"],
                                  "program": p["id"], "profile": p["profile"], "killed": killed, "how": how,
                                  "first_diff": first}) + "\n")
        out.flush()
        n = sum(r[0] for r in res)
        print(f"mutant {a.kind} {m['dir']} ({m['split']}): killed by {n}/{len(res)} programs", flush=True)
        subprocess.run(["rm", "-rf", str(work / f"m{m['dir']}")])
    out.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pool"); p.add_argument("--core", required=True); p.add_argument("--seed", type=int, default=7)
    p.add_argument("--profiles", default="base:120,ext:120,llmflash:120"); p.add_argument("--out", required=True)
    p.add_argument("--from-samples", nargs="*", help="sample files from rocket/genmodel/sample.py instead of --profiles")
    p.add_argument("--per-file", type=int, default=0,
                   help="with --from-samples: keep the first N gate-passing samples of each file (0 = all)")
    l = sub.add_parser("label")
    for k in ("--core", "--kind", "--pool", "--out"):
        l.add_argument(k, required=True)
    l.add_argument("--manifest", help="training/dev mutants (from the factory); not needed with --published")
    l.add_argument("--published", action="store_true", help="label the published Encarsia bugs instead")
    l.add_argument("--limit-programs", type=int, default=0, help="first N pool programs only (tests)")
    l.add_argument("--shard", type=int, default=0); l.add_argument("--of", type=int, default=1)
    l.add_argument("--jobs", type=int, default=4)
    l.add_argument("--split", choices=["all", "train", "dev"], default="all", help="label only these mutants")
    l.add_argument("--only-unkilled", nargs="*", help="label only mutants no program in these label files kills")
    l.add_argument("--max-mutants", type=int, default=0, help="a fixed random subset of at most N of the selected mutants")
    l.add_argument("--only-mutants", default="", help="comma-separated mutant dirs: label only these")
    l.add_argument("--prog-shard", type=int, default=0); l.add_argument("--prog-of", type=int, default=1,
                                                                        help="label only every prog-of-th pool program")
    l.add_argument("--match-class", action="store_true",
                   help="bug-targeted evaluation: label each mutant only against programs sampled for its location "
                        "class (pool profile contains @<class>_)")
    l.add_argument("--mutants", default=str(Path.home() / "ext" / "mutants"))
    l.add_argument("--yosys", default=None, help="default: the injector build the factory used for this kind")
    a = ap.parse_args()
    cmd_pool(a) if a.cmd == "pool" else cmd_label(a)


if __name__ == "__main__":
    main()
