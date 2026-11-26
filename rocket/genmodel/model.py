"""Decoder-only transformer for program bodies (Track 1). Plain GPT: learned positions, pre-norm, GELU MLP, tied
embeddings, PyTorch scaled-dot-product attention. Default size ~85M parameters (12 x 768), context 1024 tokens; a
program body of ~40 instructions is ~400 ISA-field tokens (rocket/isa_tokenizer.py)."""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class Config:
    vocab: int = 640
    ctx: int = 1024
    layers: int = 12
    heads: int = 12
    dim: int = 768
    dropout: float = 0.0

    @staticmethod
    def preset(name: str, vocab: int) -> "Config":
        return {"tiny": Config(vocab, 256, 2, 2, 64), "small": Config(vocab, 1024, 6, 8, 384),
                "base": Config(vocab, 1024, 12, 12, 768)}[name]


class Block(nn.Module):
    def __init__(self, c: Config):
        super().__init__()
        self.n1, self.n2 = nn.LayerNorm(c.dim), nn.LayerNorm(c.dim)
        self.qkv, self.proj = nn.Linear(c.dim, 3 * c.dim), nn.Linear(c.dim, c.dim)
        self.mlp = nn.Sequential(nn.Linear(c.dim, 4 * c.dim), nn.GELU(), nn.Linear(4 * c.dim, c.dim))
        self.heads, self.drop = c.heads, c.dropout

    def forward(self, x):
        b, t, d = x.shape
        q, k, v = self.qkv(self.n1(x)).view(b, t, 3, self.heads, d // self.heads).permute(2, 0, 3, 1, 4)
        a = F.scaled_dot_product_attention(q, k, v, is_causal=True, dropout_p=self.drop if self.training else 0.0)
        x = x + self.proj(a.transpose(1, 2).reshape(b, t, d))
        return x + self.mlp(self.n2(x))


class GPT(nn.Module):
    def __init__(self, c: Config):
        super().__init__()
        self.c = c
        self.tok, self.pos = nn.Embedding(c.vocab, c.dim), nn.Embedding(c.ctx, c.dim)
        self.blocks = nn.ModuleList(Block(c) for _ in range(c.layers))
        self.norm = nn.LayerNorm(c.dim)
        self.apply(self._init)
        for n, p in self.named_parameters():
            if n.endswith("proj.weight") or n.endswith("mlp.2.weight"):
                nn.init.normal_(p, 0.0, 0.02 / math.sqrt(2 * c.layers))

    @staticmethod
    def _init(m):
        if isinstance(m, (nn.Linear, nn.Embedding)):
            nn.init.normal_(m.weight, 0.0, 0.02)
        if isinstance(m, nn.Linear) and m.bias is not None:
            nn.init.zeros_(m.bias)

    def params(self) -> int:
        return sum(p.numel() for p in self.parameters()) - self.pos.weight.numel()

    def forward(self, idx):
        t = idx.shape[1]
        x = self.tok(idx) + self.pos(torch.arange(t, device=idx.device))
        for blk in self.blocks:
            x = blk(x)
        return self.norm(x) @ self.tok.weight.T            # tied output embedding

    def logprob(self, idx, mask):
        """Sum of log p(token) over positions where mask is 1 (targets are idx shifted by one)."""
        logits = self(idx[:, :-1]).float()
        lp = torch.gather(F.log_softmax(logits, -1), 2, idx[:, 1:, None]).squeeze(-1)
        return (lp * mask[:, 1:]).sum(-1)

    @torch.no_grad()
    def generate(self, prefix: list[int], max_new: int, eos: int, temperature: float = 1.0, top_k: int = 0,
                 n: int = 1, device: str = "cpu") -> list[list[int]]:
        x = torch.tensor([prefix] * n, device=device)
        done = torch.zeros(n, dtype=torch.bool, device=device)
        for _ in range(max_new):
            logits = self(x[:, -self.c.ctx:])[:, -1].float() / max(temperature, 1e-5)
            if top_k:
                kth = torch.topk(logits, top_k).values[:, -1:]
                logits = logits.masked_fill(logits < kth, float("-inf"))
            nxt = torch.multinomial(F.softmax(logits, -1), 1)
            nxt[done] = eos
            x = torch.cat([x, nxt], 1)
            done |= nxt.squeeze(1) == eos
            if done.all():
                break
        return [row[len(prefix):] for row in x.tolist()]

    def save(self, path, **extra):
        torch.save({"config": asdict(self.c), "state": self.state_dict(), **extra}, path)

    @staticmethod
    def load(path, device="cpu") -> "GPT":
        ck = torch.load(path, map_location=device, weights_only=False)
        m = GPT(Config(**ck["config"]))
        m.load_state_dict(ck["state"])
        return m.to(device)
