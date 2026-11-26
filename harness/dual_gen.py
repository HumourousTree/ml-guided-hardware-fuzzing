"""Paired-program generator for the dual-hart system (docs/DUAL_HART.md, "Stimulus").

Each hart gets a random sequence of blocks:
  work      private ALU / MUL / DIV work of random length (changes the hart's timing, hence the interleaving)
  shared    loads and stores to a small set of shared words (conflicts are likely)
  amo       amoswap.w on shared words
  critical  spinlock-protected read-modify-write of a shared counter (bounded retries)
  burst     both harts hammer one hot shared word (same-address collisions)
  handshake producer/consumer: hart 0 writes data then a flag, hart 1 waits for the flag (bounded) and reads the data
Every loop is bounded, so a correct system always terminates; programs fall through to the zero word, which traps.
Registers: s0 = shared base 0x8000, s1 = lock word, a0-a7 / t0-t6 data and scratch.
"""
from __future__ import annotations

import random

from harness.asm import assemble

SHARED_WORDS = 16                     # 0x8000-0x803C: data words; 0x8040: lock; 0x8044: counter; 0x8048/0x804C: flag/data
DATA_REGS = ["a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7"]
ALU = ["add", "sub", "xor", "or", "and", "sll", "srl", "sra", "slt", "sltu", "mul", "mulh", "mulhu", "div", "divu", "rem", "remu"]


class _Prog:
    def __init__(self, rng: random.Random, hart: int):
        self.rng, self.hart, self.lines, self.n = rng, hart, [], 0

    def label(self, stem: str) -> str:
        self.n += 1
        return f"{stem}_{self.n}"

    def emit(self, *ls: str) -> None:
        self.lines.extend(ls)


def _work(p: _Prog) -> None:
    for _ in range(p.rng.randint(1, 12)):
        d, a, b = (p.rng.choice(DATA_REGS) for _ in range(3))
        if p.rng.random() < 0.3:
            p.emit(f"addi {d}, {a}, {p.rng.randint(-2048, 2047)}")
        else:
            p.emit(f"{p.rng.choice(ALU)} {d}, {a}, {b}")


def _shared(p: _Prog) -> None:
    for _ in range(p.rng.randint(1, 6)):
        off = 4 * p.rng.randrange(SHARED_WORDS)
        r = p.rng.choice(DATA_REGS)
        if p.rng.random() < 0.5:
            p.emit(f"sw {r}, {off}(s0)")
        else:
            op = p.rng.choice(["lw", "lw", "lh", "lhu", "lb", "lbu"])
            p.emit(f"{op} {r}, {off + (p.rng.randrange(4) if op in ('lb', 'lbu') else 0)}(s0)")
        if p.rng.random() < 0.2:
            p.emit(f"sb {r}, {off + p.rng.randrange(4)}(s0)")


def _amo(p: _Prog) -> None:
    off = 4 * p.rng.randrange(SHARED_WORDS)
    d, s = p.rng.choice(DATA_REGS), p.rng.choice(DATA_REGS)
    p.emit(f"addi t4, s0, {off}", f"amoswap.w {d}, {s}, (t4)")


def _critical(p: _Prog) -> None:
    acq, got, skip = p.label("acq"), p.label("got"), p.label("skip")
    p.emit("li t3, 64", "li t2, 1",
           f"{acq}:", "amoswap.w t0, t2, (s1)", f"beqz t0, {got}", "addi t3, t3, -1", f"bnez t3, {acq}", f"j {skip}",
           f"{got}:", "lw t1, 4(s1)", f"addi t1, t1, {p.hart + 1}", "sw t1, 4(s1)", "sw zero, 0(s1)",
           f"{skip}:")


def _handshake(p: _Prog) -> None:
    if p.hart == 0:
        r = p.rng.choice(DATA_REGS)
        p.emit(f"sw {r}, 12(s1)", "li t5, 1", "sw t5, 8(s1)")
    else:
        wait, done = p.label("wait"), p.label("hs")
        p.emit("li t3, 64", f"{wait}:", "lw t5, 8(s1)", f"bnez t5, {done}", "addi t3, t3, -1", f"bnez t3, {wait}",
               f"{done}:", f"lw {p.rng.choice(DATA_REGS)}, 12(s1)")


def _burst(p: _Prog) -> None:
    """Both harts hammer one hot word (0x8030), so their data requests collide on the same address."""
    for _ in range(p.rng.randint(4, 10)):
        r = p.rng.choice(DATA_REGS)
        p.emit(f"sw {r}, 48(s0)" if p.rng.random() < 0.6 else f"lw {r}, 48(s0)")


BLOCKS = {"work": (_work, 4), "shared": (_shared, 4), "amo": (_amo, 2), "critical": (_critical, 2), "handshake": (_handshake, 1),
          "burst": (_burst, 2)}


def gen_hart(rng: random.Random, hart: int, blocks: int) -> str:
    p = _Prog(rng, hart)
    p.emit("li s0, 0x8000", "li s1, 0x8040")
    for r in DATA_REGS:
        p.emit(f"li {r}, {rng.randint(-2**31, 2**31 - 1)}")
    names, weights = zip(*((k, w) for k, (_, w) in BLOCKS.items()))
    for _ in range(blocks):
        BLOCKS[rng.choices(names, weights)[0]][0](p)
    return "\n".join(p.lines) + "\n"


def gen_pair(seed: int, blocks: int = 24) -> dict:
    rng = random.Random(seed)
    src = [gen_hart(rng, h, rng.randint(blocks // 2, blocks)) for h in (0, 1)]
    return {"seed": seed, "src": src, "words": [assemble(s) for s in src]}
