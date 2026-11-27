"""Sample program bodies from a checkpoint and measure how many are usable (Track 1).

  python -m rocket.genmodel.sample --ckpt ck/pre.pt --core rocket --profile base --n 64 [--kill] [--assemble] --out s.json

Stages of usability, each reported as a rate:
  decoded    the token stream decodes to assembly lines
  static     the harness rules hold (no x28/x29/x31, x30 never written, no harness CSRs/labels, branches forward)
  assembled  the wrapped program assembles and links (needs the RISC-V toolchain; --assemble)
The full gate (Spike and the bug-free reference agree and stop) and mutant kills are measured by
rocket/label_mutants.py on the cloud runners, from the programs this script writes.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import tempfile
import time
from pathlib import Path

import torch

from rocket.genmodel.data import ROOT, tokenizer
from rocket.genmodel import grammar
from rocket.genmodel.model import GPT

sys.path.insert(0, str(ROOT / "rocket"))
import rv64fuzz  # noqa: E402

HARNESS = re.compile(r"\b(mtvec|tohost|fromhost|satp|pmpcfg\d*|pmpaddr\d*|medeleg|mideleg|_start)\b")
NO_RD = ("sb", "sh", "sw", "sd", "fsw", "fsd", "c.sw", "c.sd", "c.fsw", "c.fsd", "c.swsp", "c.sdsp", "j", "c.j", "c.jr",
         "csrw", "csrs", "csrc", "csrwi", "csrsi", "csrci", "fence", "fence.i", "ecall", "ebreak", "mret", "c.nop", "nop",
         "c.beqz", "c.bnez", "c.ebreak", "wfi")


def dest(mn: str, ops: list[str]) -> str | None:
    """Integer destination register of an instruction, if it has one."""
    if mn.startswith(".") or mn in NO_RD or (mn.startswith("b") and mn not in ("bclr", "bset")) or not ops:
        return None
    return ops[0] if re.fullmatch(r"x\d+", ops[0]) else None


def static_check(body: list[str], profile: str) -> str | None:
    """The harness rules every generator body obeys (calibrated: all generator bodies pass)."""
    reserved = {"x30", "x31"} | ({"x29"} if profile != "base" else set())   # x29: trap accumulator; x28 is scratch
    defined = set()
    for l in body:
        t = l.strip()
        m = re.match(r"^(\w+):$", t)
        if m:
            defined.add(m.group(1)); continue
        if HARNESS.search(t):
            return f"harness symbol: {t}"
        parts = t.split(None, 1)
        mn, ops = parts[0].lower(), [o.strip() for o in re.split(r",(?![^(]*\))", parts[1])] if len(parts) > 1 else []
        d = dest(mn, ops)
        if d in reserved:
            return f"writes reserved {d}: {t}"
        if mn != "la":
            for ref in re.findall(r"\b(L\d+)\b", t):
                if ref in defined:
                    return f"backward branch: {t}"
    return None


LABEL_REF = re.compile(r"\b(L\d+|T\d+_A)\b")


def close_body(body: list[str]) -> list[str]:
    """A body cut off by the context limit can leave forward references or `.option push` open: define the missing
    labels at the end (the branch then jumps to the end of the body) and pop what was pushed."""
    defined = {m.group(1) for l in body for m in [re.match(r"(\w+):", l)] if m}      # also `T1_A: .dword ...`
    refs = []
    for l in body:
        if not l.endswith(":"):                     # branch targets and `la` template labels
            refs += [x for x in LABEL_REF.findall(l) if x not in defined and x not in refs]
    depth = 0
    for l in body:
        depth += l == ".option push"
        depth -= l == ".option pop" and depth > 0
    return body + [".option pop"] * depth + [f"{x}:" for x in refs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", type=Path, required=True); ap.add_argument("--core", default="rocket")
    ap.add_argument("--profile", default="base"); ap.add_argument("--kill", action="store_true")
    ap.add_argument("--n", type=int, default=64); ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--top-k", type=int, default=0); ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--no-grammar", action="store_true", help="unconstrained sampling (default: grammar-constrained)")
    ap.add_argument("--assemble", action="store_true"); ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    torch.manual_seed(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tk = tokenizer()
    model = GPT.load(a.ckpt, dev).eval()
    prefix = [tk.id["<bos>"], tk.id[f"ctl:core:{a.core}"], tk.id[f"ctl:prof:{a.profile}"]] + ([tk.id["ctl:kill"]] if a.kill else [])
    # fp16 autocast on the GPU (several times faster on a T4; logits are cast back to fp32 before sampling), in
    # batches so memory stays bounded for long contexts
    # grammar-constrained decoding (rocket/genmodel/grammar.py): every sampled body is well-formed by construction
    gram = None if a.no_grammar else grammar.load()
    outs, t0 = [], time.time()
    with torch.autocast("cuda", dtype=torch.float16, enabled=dev == "cuda"):
        for k in range(0, a.n, a.batch):
            m = min(a.batch, a.n - k)
            cons = None if gram is None else [grammar.Constraint(gram, tk, a.profile) for _ in range(m)]
            outs += model.generate(prefix, model.c.ctx - len(prefix), tk.id["<eos>"], a.temperature, a.top_k,
                                   m, dev, constraints=cons)
    gen_seconds = time.time() - t0
    q = rv64fuzz.QUIRKS[a.core]
    rows, w = [], Path(tempfile.mkdtemp(prefix="gmsample-"))
    for i, ids in enumerate(outs):
        r = {"i": i, "decoded": False, "static": None, "assembled": None}
        try:
            body = [l.strip() for l in tk.decode(ids).splitlines() if l.strip()]
            r["decoded"] = bool(body)
        except Exception as e:                                   # noqa: BLE001 - any decode failure is a sample failure
            r["error"] = f"decode: {e}"; rows.append(r); continue
        body = close_body(body)
        r["body"] = body
        r["static"] = static_check(body, a.profile)
        if r["static"] is None:
            asm = rv64fuzz.gen_program(random.Random(a.seed * 100003 + i), 40, True, a.profile, q.get("subst"), None,
                                       q.get("keep_frm_valid", False), q.get("template_subst"), body=body)
            r["asm"] = asm
            if a.assemble:
                try:
                    rv64fuzz.build_elf(asm, w); r["assembled"] = True
                except Exception as e:                           # noqa: BLE001
                    r["assembled"] = False
                    r["error"] = (getattr(e, "stderr", b"") or b"").decode(errors="replace")[-300:]
        rows.append(r)
    n = len(rows)
    summary = {"n": n, "decoded": sum(r["decoded"] for r in rows) / n,
               "static_ok": sum(r["static"] is None and r["decoded"] for r in rows) / n,
               "mean_lines": sum(len(r.get("body", [])) for r in rows) / n, "grammar": gram is not None,
               "gen_seconds": round(gen_seconds, 1)}
    if a.assemble:
        summary["assembled"] = sum(bool(r["assembled"]) for r in rows) / n
    a.out.write_text(json.dumps({"summary": summary, "samples": rows}, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
