"""Train the program-body generator (Track 1). Runs on CPU (tiny smoke tests) or one/two GPUs (Kaggle).

  python -m rocket.genmodel.train pretrain --data data/pretrain.npz --preset base --steps 20000 --out ck/pre.pt
  python -m rocket.genmodel.train sft  --data data/sft.npz --init ck/pre.pt --steps 2000 --out ck/sft.pt
  python -m rocket.genmodel.train dpo  --pairs data/sft.pairs.json --init ck/sft.pt --steps 1000 --out ck/dpo.pt

pretrain: next-token loss on generator bodies. sft: same loss, sequences sampled in proportion to the number of
training mutants they kill. dpo: direct preference optimisation on (killer, non-killer) pairs against the frozen init.
Kills only count for programs that passed the pool gate (Spike and the bug-free reference agree and stop), so a
program cannot "kill" by hanging or by an implementation-defined difference.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from rocket.genmodel.data import tokenizer
from rocket.genmodel.model import GPT, Config


def batches(data: dict, bs: int, ctx: int, pad: int, rng: np.random.Generator, weights=None):
    offs = np.concatenate([[0], np.cumsum(data["lengths"])])
    n = len(data["lengths"])
    p = None if weights is None else weights / weights.sum()
    while True:
        idx = rng.choice(n, bs, p=p)
        x = np.full((bs, ctx), pad, dtype=np.int64)
        m = np.zeros((bs, ctx), dtype=np.float32)
        for r, i in enumerate(idx):
            s = data["tokens"][offs[i]:offs[i + 1]][:ctx]
            x[r, :len(s)] = s; m[r, :len(s)] = 1
        yield torch.from_numpy(x), torch.from_numpy(m)


def packed(data: dict, bs: int, ctx: int, rng: np.random.Generator):
    """Pretraining batches: random ctx-token windows of the concatenated corpus (each sequence starts with <bos> and
    ends with <eos>, so boundaries stay visible). No padding."""
    flat = data["tokens"]
    hi = len(flat) - ctx - 1
    while True:
        starts = rng.integers(0, hi, bs)
        x = np.stack([flat[s:s + ctx] for s in starts]).astype(np.int64)
        yield torch.from_numpy(x), torch.ones((bs, ctx), dtype=torch.float32)


def lm_loss(model, x, m):
    logits = model(x[:, :-1])
    return (F.cross_entropy(logits.float().transpose(1, 2), x[:, 1:], reduction="none") * m[:, 1:]).sum() / m[:, 1:].sum()


def schedule(step, steps, lr, warm):
    if step < warm:
        return lr * (step + 1) / warm
    return lr * 0.1 + lr * 0.9 * 0.5 * (1 + math.cos(math.pi * (step - warm) / max(1, steps - warm)))


def pad_pairs(pairs, ctx, pad):
    x = torch.full((len(pairs), ctx), pad, dtype=torch.long)
    m = torch.zeros((len(pairs), ctx))
    for r, s in enumerate(pairs):
        s = s[:ctx]; x[r, :len(s)] = torch.tensor(s); m[r, :len(s)] = 1
    return x, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["pretrain", "sft", "dpo"])
    ap.add_argument("--data", type=Path); ap.add_argument("--pairs", type=Path)
    ap.add_argument("--init", type=Path); ap.add_argument("--preset", default="base")
    ap.add_argument("--steps", type=int, default=10000); ap.add_argument("--bs", type=int, default=32)
    ap.add_argument("--accum", type=int, default=1); ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--warmup", type=int, default=500); ap.add_argument("--beta", type=float, default=0.1)
    ap.add_argument("--val-frac", type=float, default=0.01); ap.add_argument("--eval-every", type=int, default=500)
    ap.add_argument("--seed", type=int, default=1); ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    torch.manual_seed(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # native bf16 only from Ampere on: torch.cuda.is_bf16_supported() also counts emulation, which on a T4 is slow and
    # sends attention to the memory-hungry math kernel; T4s get fp16 with a loss scaler instead
    bf16 = dev == "cuda" and torch.cuda.get_device_capability()[0] >= 8
    amp = torch.autocast(dev, dtype=torch.bfloat16 if bf16 else torch.float16, enabled=dev == "cuda")
    tk = tokenizer()
    pad = tk.id["<pad>"]
    model = GPT.load(a.init, dev) if a.init else GPT(Config.preset(a.preset, len(tk))).to(dev)
    if model.c.vocab < len(tk):          # control tokens added after pretraining (bug-location tokens)
        print(json.dumps({"grow_vocab": [model.c.vocab, len(tk)]}), flush=True)
        model.grow_vocab(len(tk))
    ctx = model.c.ctx
    print(json.dumps({"stage": a.stage, "params": model.params(), "device": dev, "gpus": torch.cuda.device_count()}), flush=True)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, betas=(0.9, 0.95), weight_decay=0.1)
    scaler = torch.amp.GradScaler(enabled=dev == "cuda" and not bf16)
    rng = np.random.default_rng(a.seed)
    log, t0 = [], time.time()

    if a.stage in ("pretrain", "sft"):
        d = dict(np.load(a.data))
        n = len(d["lengths"]); nval = max(1, int(n * a.val_frac))
        offs = np.concatenate([[0], np.cumsum(d["lengths"])])
        val = {"tokens": d["tokens"][:offs[nval]], "lengths": d["lengths"][:nval]}
        tr = {"tokens": d["tokens"][offs[nval]:], "lengths": d["lengths"][nval:]}
        w = d["weights"][nval:] if a.stage == "sft" and "weights" in d else None
        # pretrain: packed windows (validation too, so the losses are comparable); sft: whole weighted sequences
        it = packed(tr, a.bs, ctx, rng) if a.stage == "pretrain" else batches(tr, a.bs, ctx, pad, rng, w)
        vit = (packed(val, a.bs, ctx, np.random.default_rng(0)) if a.stage == "pretrain"
               else batches(val, a.bs, ctx, pad, np.random.default_rng(0)))
        vbatches = [next(vit) for _ in range(8)]
        for step in range(a.steps):
            for g in opt.param_groups:
                g["lr"] = schedule(step, a.steps, a.lr, a.warmup)
            for _ in range(a.accum):
                x, m = (t.to(dev) for t in next(it))
                with amp:
                    loss = lm_loss(model, x, m) / a.accum
                scaler.scale(loss).backward()
            scaler.unscale_(opt); torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt); scaler.update(); opt.zero_grad(set_to_none=True)
            if step % a.eval_every == 0 or step == a.steps - 1:
                model.eval()
                with torch.no_grad(), amp:
                    vl = sum(lm_loss(model, x.to(dev), m.to(dev)).item() for x, m in vbatches) / len(vbatches)
                model.train()
                log.append({"step": step, "train_loss": round(loss.item() * a.accum, 4), "val_loss": round(vl, 4),
                            "seconds": round(time.time() - t0)})
                print(json.dumps(log[-1]), flush=True)
                model.save(a.out, stage=a.stage, log=log)
    else:
        pairs = json.loads(a.pairs.read_text())
        ref = GPT.load(a.init, dev).eval()
        if ref.c.vocab < model.c.vocab:      # same rows as the policy's grown embedding (new tokens start equal)
            ref.tok = type(model.tok)(model.c.vocab, model.c.dim).to(dev); ref.tok.weight.data.copy_(model.tok.weight.data); ref.c.vocab = model.c.vocab
        for p in ref.parameters():
            p.requires_grad_(False)
        order = rng.permutation(len(pairs))
        for step in range(a.steps):
            for g in opt.param_groups:
                g["lr"] = schedule(step, a.steps, a.lr, min(a.warmup, a.steps // 10))
            sel = [pairs[order[(step * a.bs + j) % len(pairs)]] for j in range(a.bs)]
            xw, mw = (t.to(dev) for t in pad_pairs([s[0] for s in sel], ctx, pad))
            xl, ml = (t.to(dev) for t in pad_pairs([s[1] for s in sel], ctx, pad))
            with amp:
                with torch.no_grad():
                    rw, rl = ref.logprob(xw, mw), ref.logprob(xl, ml)
                pw, pl = model.logprob(xw, mw), model.logprob(xl, ml)
                margin = a.beta * ((pw - rw) - (pl - rl))
                loss = -F.logsigmoid(margin).mean()
            scaler.scale(loss).backward()
            scaler.unscale_(opt); torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt); scaler.update(); opt.zero_grad(set_to_none=True)
            if step % a.eval_every == 0 or step == a.steps - 1:
                log.append({"step": step, "dpo_loss": round(loss.item(), 4),
                            "pref_acc": round((margin > 0).float().mean().item(), 3), "seconds": round(time.time() - t0)})
                print(json.dumps(log[-1]), flush=True)
                model.save(a.out, stage=a.stage, log=log)
    a.out.with_suffix(".log.json").write_text(json.dumps(log, indent=1))


if __name__ == "__main__":
    main()
