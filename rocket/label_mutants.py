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


def cmd_pool(a):
    work = Path(tempfile.mkdtemp(prefix="pool-"))
    ref = _build(mutant_factory.CORPUS / a.core / "multiplexer" / "reference.rtlil", work / "ref", a.core, True)
    progs, dropped = [], 0
    for spec in a.profiles.split(","):
        prof, n = spec.split(":")
        rng = random.Random(f"{a.seed}-{a.core}-{prof}")
        for i in range(int(n)):
            asm = _gen(a.core, prof, rng)
            with tempfile.TemporaryDirectory() as w:
                elf = rv64fuzz.build_elf(asm, Path(w))
                try:
                    exp = rv64fuzz.run_spike(elf)
                except Exception:
                    exp = None
                got, stopped = rv64fuzz.run_rocket(ref, elf, SIMLEN) if exp else ([], False)
            if not exp or not stopped or got != exp:
                dropped += 1
                continue
            progs.append({"id": f"{prof}-{i}", "profile": prof, "asm": asm, "expected": exp})
    Path(a.out).write_text(json.dumps({"core": a.core, "seed": a.seed, "profiles": a.profiles, "simlen": SIMLEN,
                                       "dropped_reference_disagreement": dropped, "programs": progs}))
    print(f"pool {a.core}: {len(progs)} programs kept, {dropped} dropped (Spike failure, or the bug-free core "
          f"disagrees with Spike / does not stop)")


def cmd_label(a):
    man = json.loads(Path(a.manifest).read_text())
    pool = json.loads(Path(a.pool).read_text())
    assert man["core"] == pool["core"] == a.core and man["kind"] == a.kind
    mine = [m for i, m in enumerate(man["mutants"]) if i % a.of == a.shard]
    mdir = Path(a.mutants) / a.core / a.kind
    if not any(mdir.glob("*/" + mutant_factory.HOST_FILE[a.kind])):
        mutant_factory.inject(a.yosys, a.core, a.kind, man["num_bugs_requested"], mdir)
    out = open(a.out, "w")
    work = Path(tempfile.mkdtemp(prefix="label-"))
    for m in mine:
        host = mdir / str(m["dir"]) / mutant_factory.HOST_FILE[a.kind]
        if mutant_factory.sha(host) != m["sha256"]:
            raise SystemExit(f"mutant {m['dir']}: regenerated netlist differs from the manifest (injection not deterministic)")
        try:
            sim = _build(host, work / f"m{m['dir']}", a.core, a.kind == "multiplexer")
        except Exception as e:
            out.write(json.dumps({"core": a.core, "kind": a.kind, "mutant": m["dir"], "split": m["split"],
                                  "error": str(e)[:300]}) + "\n"); out.flush()
            continue
        jobs = [(str(sim), p["asm"], p["expected"]) for p in pool["programs"]]
        with ProcessPoolExecutor(a.jobs) as ex:
            res = list(ex.map(_run_one, jobs))
        for p, (killed, how, first) in zip(pool["programs"], res):
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
    l = sub.add_parser("label")
    for k in ("--core", "--kind", "--manifest", "--pool", "--out"):
        l.add_argument(k, required=True)
    l.add_argument("--shard", type=int, default=0); l.add_argument("--of", type=int, default=1)
    l.add_argument("--jobs", type=int, default=4)
    l.add_argument("--mutants", default=str(Path.home() / "ext" / "mutants"))
    l.add_argument("--yosys", default="/opt/encarsia-yosys/bin/yosys")
    a = ap.parse_args()
    cmd_pool(a) if a.cmd == "pool" else cmd_label(a)


if __name__ == "__main__":
    main()
