"""Training data for the bug-trained generator (Track 1).

Sequences are program BODIES (the part the generator chooses; rv64fuzz.gen_program wraps them in the fixed harness),
prefixed by control tokens:  <bos> ctl:core:<core> ctl:prof:<profile> [ctl:kill] body... <eos>

  pretrain   bodies sampled from the existing generators (base / ext / llmflash, with each core's quirks): teaches
             syntax and the harness rules (reserved registers, x30 read-only, forward branches only).
  sft        bodies of pool programs weighted by how many training mutants they kill (labels.jsonl), with ctl:kill.
  pairs      preference pairs for DPO: for a training mutant, a program that kills it vs one that does not.

Dev-split mutants and held-out (published Encarsia) bugs are never used for training.
  python -m rocket.genmodel.data pretrain --n 200000 --out data/pretrain.npz
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rocket"))
import rv64fuzz  # noqa: E402
from isa_tokenizer import ISATokenizer  # noqa: E402

CORES = ("rocket", "boom")
PROFILES = ("base", "ext", "llmflash")
CONTROL = [f"ctl:core:{c}" for c in CORES] + [f"ctl:prof:{p}" for p in PROFILES] + ["ctl:kill"]
# bug-location tokens (bug-targeted generation): one per module class in rocket/genmodel/modules.json (written by the
# `modules` command from the mutant locations) plus ctl:mod:other. Appended after CONTROL so earlier token ids (and
# checkpoints) are unchanged; train.py grows the embedding of an older checkpoint.
MODULES_FILE = Path(__file__).with_name("modules.json")
MODULES = json.loads(MODULES_FILE.read_text())["classes"] if MODULES_FILE.exists() else []
LOCATIONS = [ROOT / "data" / "mutants" / f"{c}_{k}_locations.json" for c in CORES for k in ("driver", "multiplexer")]


def module_class(name: str) -> str:
    """Module class of a located module: the instance suffix of Chisel-generated names is dropped (Arbiter_16 ->
    Arbiter, BranchKillableQueue_5 -> BranchKillableQueue)."""
    return re.sub(r"_\d+$", "", name)


def mod_token(name: str | None) -> str:
    c = module_class(name) if name else "other"
    return f"ctl:mod:{c}" if c in MODULES else "ctl:mod:other"


def load_locations(paths=None) -> dict:
    """(core, kind, dir) -> first located module, from the *_locations.json files that exist."""
    out = {}
    for p in paths or LOCATIONS:
        if Path(p).exists():
            d = json.loads(Path(p).read_text())
            for k, v in d["locations"].items():
                if v["modules"]:
                    out[(d["core"], d["kind"], int(k))] = v["modules"][0]
    return out


PUBLISHED_LOCATIONS = ROOT / "data" / "mutants" / "published_locations.json"


def published_locations() -> dict:
    """(core, kind, dir) -> first located module of the published Encarsia bugs (rocket/locate_mutants.py)."""
    if not PUBLISHED_LOCATIONS.exists():
        return {}
    d = json.loads(PUBLISHED_LOCATIONS.read_text())
    return {(c, k, int(n)): v["modules"][0] for c, ks in d.items() for k, ms in ks.items() for n, v in ms.items()
            if v["modules"]}


def target_classes(core: str) -> list[str]:
    """Location classes to sample for in a bug-targeted evaluation: those of the core's dev mutants and published bugs
    (a class outside MODULES is sampled as "other")."""
    split = {}
    for k in ("driver", "multiplexer"):
        mf = ROOT / "data" / "mutants" / f"{core}_{k}_manifest.json"
        if mf.exists():
            split.update({(core, k, m["dir"]): m["split"] for m in json.loads(mf.read_text())["mutants"]})
    mods = [v for key, v in load_locations().items() if split.get(key) == "dev"]
    mods += [v for key, v in published_locations().items() if key[0] == core]
    return sorted({mod_token(v)[len("ctl:mod:"):] for v in mods})


def tokenizer() -> ISATokenizer:
    tk = ISATokenizer()
    for t in CONTROL + ([f"ctl:mod:{m}" for m in MODULES] + ["ctl:mod:other"] if MODULES else []):
        tk.id[t] = len(tk.vocab); tk.vocab.append(t)
    return tk


def templates(profile: str):
    if profile not in rv64fuzz.TEMPLATE_FILES:
        return None
    t = json.loads((ROOT / "rocket" / "templates" / rv64fuzz.TEMPLATE_FILES[profile]).read_text())
    return t.get("templates", t) if isinstance(t, dict) else t


def body_of(core: str, profile: str, seed: int, length: int = 40) -> list[str]:
    q = rv64fuzz.QUIRKS[core]
    _, body = rv64fuzz.gen_program(random.Random(seed), length, True, profile, q.get("subst"), templates(profile),
                                   q.get("keep_frm_valid", False), q.get("template_subst"), return_body=True)
    return body


def encode(tk: ISATokenizer, core: str, profile: str, body: list[str], kill: bool = False,
           module: str | None = None) -> list[int]:
    """module: a ctl:mod:* token (bug-targeted sequences), placed after ctl:kill."""
    ctl = ([tk.id["<bos>"], tk.id[f"ctl:core:{core}"], tk.id[f"ctl:prof:{profile}"]] + ([tk.id["ctl:kill"]] if kill else [])
           + ([tk.id[module]] if module else []))
    return ctl + tk.encode("\n".join(body), bos=False)


def pack(seqs: list[list[int]], ctx: int) -> dict:
    keep = [s[:ctx] for s in seqs]
    lens = np.array([len(s) for s in keep], dtype=np.int32)
    flat = np.concatenate([np.array(s, dtype=np.uint16) for s in keep])
    return {"tokens": flat, "lengths": lens}


def build_pretrain(n: int, seed: int, ctx: int) -> tuple[dict, dict]:
    tk = tokenizer()
    rng = random.Random(seed)
    seqs, stats = [], collections.Counter()
    for i in range(n):
        core, prof = rng.choice(CORES), rng.choice(PROFILES)
        body = body_of(core, prof, rng.getrandbits(48), rng.choice([20, 40, 60]))
        seqs.append(encode(tk, core, prof, body))
        stats[f"{core}/{prof}"] += 1
    arr = pack(seqs, ctx)
    return arr, {"sequences": n, "tokens": int(arr["tokens"].size), "mean_len": float(arr["lengths"].mean()),
                 "truncated": int((np.array([len(s) for s in seqs]) > ctx).sum()), "mix": dict(stats),
                 "vocab": len(tk)}


def body_from_asm(asm: str) -> list[str]:
    """Cut the body out of a full program: after the prologue's last line (fsrmi, FP is always on in the pool) and
    before the epilogue's first write of x31 (the body never writes x31). ext's leading `.option rvc` is harness."""
    lines = asm.splitlines()
    # the FIRST fsrmi: the prologue has exactly one, and bodies may contain their own (until 2026-11-28 this took the
    # last one, which cut the front off ~20% of the generator-pool bodies used for SFT/DPO)
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("fsrmi x0,")) + 1
    end = next(i for i in range(start, len(lines)) if lines[i].strip().startswith("li x31,"))
    body = [l.strip() for l in lines[start:end] if l.strip()]
    return body[1:] if body[:1] == [".option rvc"] else body


def pool_bodies(pool: dict) -> dict[str, list[str]]:
    return {p["id"]: p.get("body") or body_from_asm(p["asm"]) for p in pool["programs"]}


def read_text(path: Path) -> str:
    import gzip
    return gzip.open(path, "rt").read() if str(path).endswith(".gz") else Path(path).read_text()


def _profile(p: str) -> str:
    """Generator profile of a pool program; learned-generator programs carry their sample-file stem
    (samples_<run>_<core>_<profile>)."""
    m = re.search(r"_(base|ext|llmflash)$", p)
    return m.group(1) if m else p


def build_sft_pairs(labels: list[Path], pools: list[Path], ctx: int, seed: int,
                    weighting: str = "count", targeted: bool = False) -> tuple[dict, list, dict]:
    """SFT sequences (pool programs that kill >= 1 TRAIN mutant) and DPO pairs (for each train mutant: up to 4 of its
    killers, each against a random program labelled against that mutant that does not kill it). Any number of pools
    per core (the generator pools, and pools of the model's own samples for on-policy rounds); programs are keyed by
    (core, id) and mutants by (core, kind, dir), so nothing collides across cores or bug kinds; dev mutants are not
    read. weighting: "count" = number of train mutants killed; "rarity" = sum over them of 1 / (number of killers),
    so a program that kills a mutant almost nothing else kills outweighs one that kills many easy ones.
    targeted: also condition on where the bug is. Each killer additionally appears once per module class of the train
    mutants it kills, with that ctl:mod token and the weight of those kills only, and DPO pairs carry the mutant's
    token. The untargeted sequences are kept, so the model can still be sampled without a location."""
    tk = tokenizer()
    cores = set()
    bodies, prof = {}, {}
    for pp in pools:
        d = json.loads(read_text(pp))
        cores.add(d["core"])
        for pid, b in pool_bodies(d).items():
            assert (d["core"], pid) not in bodies or bodies[(d["core"], pid)] == b, f"program id {pid} in two pools"
            bodies[(d["core"], pid)] = b
        for p in d["programs"]:
            prof[(d["core"], p["id"])] = _profile(p["profile"])
    killers = collections.defaultdict(set)
    labelled, seen = collections.defaultdict(set), collections.Counter()
    for lp in labels:
        for line in read_text(lp).splitlines():
            r = json.loads(line)
            if "killed" not in r or r["split"] != "train":
                continue
            core = r["core"]
            assert core in cores, f"no pool for core {core}"
            key, m = (core, r["program"]), (core, r["kind"], r["mutant"])
            seen[f"{core}/{r['kind']}"] += 1
            labelled[m].add(key)
            if r["killed"]:
                killers[m].add(key)
    loc = load_locations() if targeted else {}
    assert not targeted or (loc and MODULES), "targeted needs data/mutants/*_locations.json and genmodel/modules.json"
    mtok = lambda m: mod_token(loc.get((m[0], m[1], int(m[2]))))
    kills = collections.defaultdict(float)
    tkills = collections.defaultdict(float)                     # (program key, ctl:mod token) -> weight
    for m, ks in killers.items():
        for key in ks:
            wt = 1.0 / len(ks) if weighting == "rarity" else 1.0
            kills[key] += wt
            if targeted:
                tkills[(key, mtok(m))] += wt
    sft, w = [], []
    for key, k in sorted(kills.items()):
        sft.append(encode(tk, key[0], prof[key], bodies[key], kill=True)); w.append(k)
    for (key, mt), k in sorted(tkills.items()):
        sft.append(encode(tk, key[0], prof[key], bodies[key], kill=True, module=mt)); w.append(k)
    rng = random.Random(seed)
    pairs = []
    for m, ks in sorted(killers.items()):
        losers = sorted(labelled[m] - ks)
        if not losers:
            continue
        for win in rng.sample(sorted(ks), min(4, len(ks))):
            lose = rng.choice(losers)
            mt = mtok(m) if targeted else None
            pairs.append((encode(tk, win[0], prof[win], bodies[win], True, mt)[:ctx],
                          encode(tk, lose[0], prof[lose], bodies[lose], True, mt)[:ctx]))
    rng.shuffle(pairs)
    arr = pack(sft, ctx)
    arr["weights"] = np.array(w, dtype=np.float32)
    return arr, pairs, {"cores": sorted(cores), "weighting": weighting, "label_rows": dict(seen), "sft_programs": len(kills), "sft_sequences": len(sft), "targeted": targeted,
                        "targeted_by_module": dict(collections.Counter(mt for _, mt in tkills)),
                        "pairs": len(pairs), "train_mutants_with_killers": len(killers),
                        "sft_programs_by_core_profile": dict(collections.Counter(f"{k[0]}/{prof[k]}" for k in kills))}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pretrain"); p.add_argument("--n", type=int, default=200_000); p.add_argument("--seed", type=int, default=1)
    p.add_argument("--ctx", type=int, default=1024); p.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("sft"); s.add_argument("--labels", type=Path, nargs="+", required=True); s.add_argument("--pool", type=Path, nargs="+", required=True)
    s.add_argument("--ctx", type=int, default=1024); s.add_argument("--seed", type=int, default=1); s.add_argument("--out", type=Path, required=True)
    s.add_argument("--weighting", choices=["count", "rarity"], default="count")
    s.add_argument("--targeted", action="store_true", help="add bug-location (ctl:mod) conditioned sequences")
    mo = sub.add_parser("modules", help="write genmodel/modules.json: module classes with >= --min train mutants")
    mo.add_argument("--min", type=int, default=5); mo.add_argument("--out", type=Path, default=MODULES_FILE)
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if a.cmd == "modules":
        split = {}
        for c in CORES:
            for k in ("driver", "multiplexer"):
                mf = ROOT / "data" / "mutants" / f"{c}_{k}_manifest.json"
                if mf.exists():
                    split.update({(c, k, m["dir"]): m["split"] for m in json.loads(mf.read_text())["mutants"]})
        cnt = collections.Counter(module_class(v) for key, v in load_locations().items() if split.get(key) == "train")
        classes = sorted(c for c, n in cnt.items() if n >= a.min)
        a.out.write_text(json.dumps({"min_train_mutants": a.min, "classes": classes,
                                     "train_counts": dict(cnt.most_common())}, indent=1))
        print(f"{len(classes)} classes (>= {a.min} train mutants) of {len(cnt)}: {classes}")
        return
    if a.cmd == "pretrain":
        arr, info = build_pretrain(a.n, a.seed, a.ctx)
        np.savez_compressed(a.out, **arr)
    else:
        arr, pairs, info = build_sft_pairs(a.labels, a.pool, a.ctx, a.seed, a.weighting, a.targeted)
        np.savez_compressed(a.out, **arr)
        a.out.with_suffix(".pairs.json").write_text(json.dumps(pairs))
    a.out.with_suffix(".json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info))


if __name__ == "__main__":
    main()
