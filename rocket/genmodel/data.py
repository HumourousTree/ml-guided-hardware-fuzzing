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


def build_sft_pairs(labels: Path, pool: Path, ctx: int, seed: int) -> tuple[dict, list, dict]:
    tk = tokenizer()
    pool_d = json.loads(Path(pool).read_text())
    bodies = pool_bodies(pool_d)
    prof = {p["id"]: p["profile"] for p in pool_d["programs"]}
    kills, killers = collections.Counter(), collections.defaultdict(set)
    progs_all = set()
    for line in Path(labels).read_text().splitlines():
        r = json.loads(line)
        if "killed" not in r or r["split"] != "train":
            continue
        progs_all.add(r["program"])
        if r["killed"]:
            kills[r["program"]] += 1; killers[r["mutant"]].add(r["program"])
    core = pool_d["core"]
    sft, w = [], []
    for pid, k in kills.items():
        sft.append(encode(tk, core, prof[pid], bodies[pid], kill=True)); w.append(k)
    rng = random.Random(seed)
    pairs = []
    for m, ks in killers.items():
        losers = sorted(progs_all - ks)
        if not losers:
            continue
        for win in rng.sample(sorted(ks), min(4, len(ks))):
            lose = rng.choice(losers)
            pairs.append((encode(tk, core, prof[win], bodies[win], True)[:ctx], encode(tk, core, prof[lose], bodies[lose], True)[:ctx]))
    arr = pack(sft, ctx)
    arr["weights"] = np.array(w, dtype=np.float32)
    return arr, pairs, {"sft_programs": len(sft), "pairs": len(pairs), "mutants_with_killers": len(killers)}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pretrain"); p.add_argument("--n", type=int, default=200_000); p.add_argument("--seed", type=int, default=1)
    p.add_argument("--ctx", type=int, default=1024); p.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("sft"); s.add_argument("--labels", type=Path, required=True); s.add_argument("--pool", type=Path, required=True)
    s.add_argument("--ctx", type=int, default=1024); s.add_argument("--seed", type=int, default=1); s.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if a.cmd == "pretrain":
        arr, info = build_pretrain(a.n, a.seed, a.ctx)
        np.savez_compressed(a.out, **arr)
    else:
        arr, pairs, info = build_sft_pairs(a.labels, a.pool, a.ctx, a.seed)
        np.savez_compressed(a.out, **arr)
        a.out.with_suffix(".pairs.json").write_text(json.dumps(pairs))
    a.out.with_suffix(".json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info))


if __name__ == "__main__":
    main()
