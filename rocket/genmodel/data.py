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


def tokenizer() -> ISATokenizer:
    tk = ISATokenizer()
    for t in CONTROL:
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


def encode(tk: ISATokenizer, core: str, profile: str, body: list[str], kill: bool = False) -> list[int]:
    ctl = [tk.id["<bos>"], tk.id[f"ctl:core:{core}"], tk.id[f"ctl:prof:{profile}"]] + ([tk.id["ctl:kill"]] if kill else [])
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
    start = max(i for i, l in enumerate(lines) if l.strip().startswith("fsrmi x0,")) + 1
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
                    weighting: str = "count") -> tuple[dict, list, dict]:
    """SFT sequences (pool programs that kill >= 1 TRAIN mutant) and DPO pairs (for each train mutant: up to 4 of its
    killers, each against a random program labelled against that mutant that does not kill it). Any number of pools
    per core (the generator pools, and pools of the model's own samples for on-policy rounds); programs are keyed by
    (core, id) and mutants by (core, kind, dir), so nothing collides across cores or bug kinds; dev mutants are not
    read. weighting: "count" = number of train mutants killed; "rarity" = sum over them of 1 / (number of killers),
    so a program that kills a mutant almost nothing else kills outweighs one that kills many easy ones."""
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
    kills = collections.defaultdict(float)
    for m, ks in killers.items():
        for key in ks:
            kills[key] += 1.0 / len(ks) if weighting == "rarity" else 1.0
    sft, w = [], []
    for key, k in sorted(kills.items()):
        sft.append(encode(tk, key[0], prof[key], bodies[key], kill=True)); w.append(k)
    rng = random.Random(seed)
    pairs = []
    for m, ks in sorted(killers.items()):
        losers = sorted(labelled[m] - ks)
        if not losers:
            continue
        for win in rng.sample(sorted(ks), min(4, len(ks))):
            lose = rng.choice(losers)
            pairs.append((encode(tk, win[0], prof[win], bodies[win], True)[:ctx],
                          encode(tk, lose[0], prof[lose], bodies[lose], True)[:ctx]))
    rng.shuffle(pairs)
    arr = pack(sft, ctx)
    arr["weights"] = np.array(w, dtype=np.float32)
    return arr, pairs, {"cores": sorted(cores), "weighting": weighting, "label_rows": dict(seen), "sft_programs": len(sft),
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
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if a.cmd == "pretrain":
        arr, info = build_pretrain(a.n, a.seed, a.ctx)
        np.savez_compressed(a.out, **arr)
    else:
        arr, pairs, info = build_sft_pairs(a.labels, a.pool, a.ctx, a.seed, a.weighting)
        np.savez_compressed(a.out, **arr)
        a.out.with_suffix(".pairs.json").write_text(json.dumps(pairs))
    a.out.with_suffix(".json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info))


if __name__ == "__main__":
    main()
