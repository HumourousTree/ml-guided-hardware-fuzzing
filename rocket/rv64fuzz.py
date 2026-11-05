#!/usr/bin/env python3
"""BenchFuzz on Rocket (RV64GC) for the Encarsia bug corpus: random RV64 programs, Spike as the reference, end-state oracle.

  python3 rocket/rv64fuzz.py --sim ~/rocket_runs/golden/Vtop_tiny_soc --programs 200 --seed 0 --out run.json

Each program
  * prologue: enable the FPU (mstatus.FS), point x30 at a 256-byte data region, give every register a random value;
  * body: --length random instructions: RV64I/M ALU (incl. *W), loads/stores in the data region, short forward branches,
    F/D arithmetic with valid rounding modes, int<->fp moves/conversions/compares, CSR ops on fcsr/frm/fflags/mscratch;
  * epilogue: store x1..x30, f0..f31 (fmv.x.d), fcsr and the 32 data dwords to MMIO 0x60000010 (Cascade's register-dump
    port), request stop (MMIO 0x60000000, Rocket testbench) and write tohost (Spike exits).
x31 is reserved as the dump pointer and is never a destination.  Oracle: the sequence of dumped values must be identical
on Rocket (Cascade's testbench prints them) and on Spike (commit log, stores to 0x60000010); a Rocket run that never
requests stop within --simlen cycles is also a detection (Cascade's non-termination criterion).
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PREFIX = Path(os.environ.get("BENCH3_PREFIX", Path.home() / "opt" / "bench3"))
ENV = dict(os.environ, PATH=f"{PREFIX / 'bin'}:{os.environ.get('PATH', '')}",
           LD_LIBRARY_PATH=f"{PREFIX / 'lib'}:{os.environ.get('LD_LIBRARY_PATH', '')}")
DUMP, STOP = 0x60000010, 0x60000000
LINK = "SECTIONS { . = 0x80000000; .text : { *(.text.init) *(.text) } . = ALIGN(0x1000); .data : { *(.data) } " \
       ". = ALIGN(0x1000); .tohost : { *(.tohost) } }\n"

R_OPS = "add sub sll slt sltu xor srl sra or and addw subw sllw srlw sraw".split()
M_OPS = "mul mulh mulhsu mulhu div divu rem remu mulw divw divuw remw remuw".split()
I_OPS = "addi slti sltiu xori ori andi addiw".split()
SH_OPS = [("slli", 63), ("srli", 63), ("srai", 63), ("slliw", 31), ("srliw", 31), ("sraiw", 31)]
LD = [("ld", 8), ("lw", 4), ("lwu", 4), ("lh", 2), ("lhu", 2), ("lb", 1), ("lbu", 1)]
ST = [("sd", 8), ("sw", 4), ("sh", 2), ("sb", 1)]
BR = "beq bne blt bge bltu bgeu".split()
RM = ["rne", "rtz", "rdn", "rup", "rmm", "dyn"]          # valid rounding modes only (5/6 would be illegal)
F3 = [f"f{op}.{p}" for op in ("add", "sub", "mul", "div") for p in ("s", "d")]
F2 = [f"fsgnj{x}.{p}" for x in ("", "n", "x") for p in ("s", "d")] + [f"f{m}.{p}" for m in ("min", "max") for p in ("s", "d")]
FCMP = [f"f{c}.{p}" for c in ("eq", "lt", "le") for p in ("s", "d")]
FCVT_TO_X = ["fcvt.w.s", "fcvt.wu.s", "fcvt.l.s", "fcvt.lu.s", "fcvt.w.d", "fcvt.wu.d", "fcvt.l.d", "fcvt.lu.d"]
FCVT_FROM_X = ["fcvt.s.w", "fcvt.s.wu", "fcvt.s.l", "fcvt.s.lu", "fcvt.d.w", "fcvt.d.wu", "fcvt.d.l", "fcvt.d.lu"]
CSRS = ["fcsr", "frm", "fflags", "mscratch"]
DATA_DWORDS = 32
SPECIAL = [0, 1, -1, 0x7FFFFFFFFFFFFFFF, -0x8000000000000000, 0x7FFFFFFF, -0x80000000, 0xFFFFFFFF, 0x80000000,
           0x3FF0000000000000, 0x7FF0000000000000, 0x7FF8000000000000, 0x3F800000]


def rnd64(rng):
    return rng.choice(SPECIAL) if rng.random() < .3 else rng.getrandbits(64) - (1 << 63)


def gen_program(rng: random.Random, length: int, fp: bool = True) -> str:
    rd = lambda: f"x{rng.randint(1, 29)}"                  # never x30 (data pointer) or x31 (dump pointer)
    rs = lambda: f"x{rng.randint(0, 31)}"
    fr = lambda: f"f{rng.randint(0, 31)}"
    body, lab = [], 0
    classes = [("alu", 30), ("imm", 20), ("mul", 12), ("mem", 18), ("br", 8)] + ([("fp", 9), ("csr", 3)] if fp else [])
    names, weights = zip(*classes)
    i = 0
    while i < length:
        c = rng.choices(names, weights)[0]
        if c == "alu":
            body.append(f"{rng.choice(R_OPS)} {rd()}, {rs()}, {rs()}")
        elif c == "mul":
            body.append(f"{rng.choice(M_OPS)} {rd()}, {rs()}, {rs()}")
        elif c == "imm":
            if rng.random() < .3:
                op, mx = rng.choice(SH_OPS); body.append(f"{op} {rd()}, {rs()}, {rng.randint(0, mx)}")
            elif rng.random() < .15:
                body.append(f"{rng.choice(['lui', 'auipc'])} {rd()}, {rng.randint(0, 0xFFFFF)}")
            else:
                body.append(f"{rng.choice(I_OPS)} {rd()}, {rs()}, {rng.randint(-2048, 2047)}")
        elif c == "mem":
            op, sz = rng.choice(LD + ST)
            off = rng.randrange(0, DATA_DWORDS * 8, sz)
            body.append(f"{op} {rd() if op[0] == 'l' else rs()}, {off}(x30)")
        elif c == "br":
            skip = rng.randint(1, 3)
            body.append(f"{rng.choice(BR)} {rs()}, {rs()}, L{lab}")
            for _ in range(skip):
                body.append(f"{rng.choice(R_OPS)} {rd()}, {rs()}, {rs()}")
            body.append(f"L{lab}:"); lab += 1; i += skip
        elif c == "fp":
            k = rng.random()
            if k < .35:
                body.append(f"{rng.choice(F3)} {fr()}, {fr()}, {fr()}, {rng.choice(RM)}")
            elif k < .5:
                body.append(f"{rng.choice(F2)} {fr()}, {fr()}, {fr()}")
            elif k < .62:
                body.append(f"{rng.choice(FCMP)} {rd()}, {fr()}, {fr()}")
            elif k < .74:
                body.append(f"{rng.choice(FCVT_TO_X)} {rd()}, {fr()}, {rng.choice(RM)}")
            elif k < .86:
                op = rng.choice(FCVT_FROM_X)          # int32 -> double is exact: the assembler takes no rounding mode
                body.append(f"{op} {fr()}, {rs()}" + ("" if op in ("fcvt.d.w", "fcvt.d.wu") else f", {rng.choice(RM)}"))
            else:
                body.append(f"{rng.choice(['fmv.d.x', 'fmv.w.x'])} {fr()}, {rs()}" if rng.random() < .5
                            else f"{rng.choice(['fmv.x.d', 'fmv.x.w', 'fclass.d', 'fclass.s'])} {rd()}, {fr()}")
        elif c == "csr":
            csr = rng.choice(CSRS)
            if rng.random() < .5:
                body.append(f"{rng.choice(['csrrw', 'csrrs', 'csrrc'])} {rd() if rng.random() < .7 else 'x0'}, {csr}, "
                            f"{rs() if rng.random() < .7 else 'x0'}")
            else:
                body.append(f"{rng.choice(['csrrwi', 'csrrsi', 'csrrci'])} {rd() if rng.random() < .7 else 'x0'}, {csr}, "
                            f"{rng.randint(0, 31)}")
            if csr in ("fcsr", "frm"):            # keep frm valid (an invalid frm makes "dyn" instructions illegal)
                body.append(f"fsrmi x0, {rng.randint(0, 4)}" if rng.random() < .5 else "csrrci x0, frm, 4")
        i += 1
    pro = ["  .section .text.init", "  .globl _start", "_start:", "  li x31, 0x6000", "  csrs mstatus, x31",
           "  la x30, data"]
    pro += [f"  li x{r}, {rnd64(rng)}" for r in range(1, 30)]
    if fp:
        pro += [f"  fmv.d.x f{r}, x{rng.randint(1, 29)}" for r in range(32)] + [f"  fsrmi x0, {rng.randint(0, 4)}"]
    epi = [f"  li x31, {DUMP}"] + [f"  sd x{r}, 0(x31)" for r in range(1, 31)]
    if fp:
        for r in range(32):
            epi += [f"  fmv.x.d x1, f{r}", "  sd x1, 0(x31)"]
        epi += ["  csrr x1, fcsr", "  sd x1, 0(x31)"]
    epi += ["  csrr x1, mscratch", "  sd x1, 0(x31)"]
    for k in range(DATA_DWORDS):
        epi += [f"  ld x1, {8 * k}(x30)", "  sd x1, 0(x31)"]
    epi += [f"  li x2, {STOP}", "  sd x0, 0(x2)", "  la x3, tohost", "  li x4, 1", "  sd x4, 0(x3)", "1: j 1b"]
    data = ["  .section .data", "  .align 3", "data:"] + [f"  .dword {rng.getrandbits(64)}" for _ in range(DATA_DWORDS)]
    toh = ["  .section .tohost, \"aw\"", "  .align 6", "  .globl tohost", "tohost: .dword 0", "  .globl fromhost",
           "fromhost: .dword 0"]
    return "\n".join(pro + ["  " + b if not b.endswith(":") else b for b in body] + epi + data + toh) + "\n"


def build_elf(asm: str, work: Path) -> Path:
    (work / "p.S").write_text(asm); (work / "link.ld").write_text(LINK)
    subprocess.run(["riscv64-unknown-elf-as", "-march=rv64imafd_zicsr", "-o", "p.o", "p.S"], cwd=work, env=ENV,
                   check=True, capture_output=True)
    subprocess.run(["riscv64-unknown-elf-ld", "-T", "link.ld", "-o", "p.elf", "p.o"], cwd=work, env=ENV, check=True,
                   capture_output=True)
    return work / "p.elf"


def run_spike(elf: Path, timeout: int = 30) -> list[int] | None:
    p = subprocess.run(["spike", "--isa=rv64gc", "-m0x80000000:0x800000,0x60000000:0x1000", "-l", "--log-commits", str(elf)],
                       env=ENV, capture_output=True, text=True, timeout=timeout)
    vals = [int(m.group(1), 16) for m in re.finditer(r"mem 0x0*60000010 0x([0-9a-f]+)", p.stderr)]
    return vals or None


def run_rocket(sim: Path, elf: Path, simlen: int, timeout: int = 300) -> tuple[list[int], bool]:
    env = dict(ENV, SIMSRAMELF=str(elf), SIMLEN=str(simlen))
    p = subprocess.run([str(sim)], env=env, capture_output=True, text=True, timeout=timeout)
    vals = [int(v, 16) for v in re.findall(r"Dump of reg [xf]\d+: 0x([0-9a-f]+)", p.stdout)]
    return vals, "Found a stop request" in p.stdout


def campaign(sim: Path, n: int, seed: int, length: int, simlen: int, fp: bool, stop_on_bug: bool, log=None) -> dict:
    rng = random.Random(seed)
    res = {"seed": seed, "length": length, "fp": fp, "programs": 0, "mismatches": 0, "nonterminating": 0,
           "spike_failures": 0, "first_detection": None, "t_start": time.time()}
    work = Path(tempfile.mkdtemp(prefix="rv64fuzz-"))
    try:
        for k in range(n):
            asm = gen_program(rng, length, fp)
            elf = build_elf(asm, work)
            exp = run_spike(elf)
            if exp is None:                 # the reference did not finish: not a valid test (counted, never a detection)
                res["spike_failures"] += 1; continue
            got, stopped = run_rocket(sim, elf, simlen)
            res["programs"] += 1
            bad = (not stopped) or got != exp
            if bad:
                kind = "nontermination" if not stopped else "mismatch"
                res["mismatches" if stopped else "nonterminating"] += 1
                first = next((i for i, (a, b) in enumerate(zip(got, exp)) if a != b), min(len(got), len(exp)))
                if res["first_detection"] is None:
                    res["first_detection"] = {"n_programs": res["programs"], "t": round(time.time() - res["t_start"], 1),
                                              "kind": kind, "first_diff_index": first, "asm": asm}
                if log:
                    print(f"[seed {seed}] program {res['programs']}: {kind} (first differing dump #{first})", file=log, flush=True)
                if stop_on_bug:
                    break
    finally:
        shutil.rmtree(work, ignore_errors=True)
    res["seconds"] = round(time.time() - res["t_start"], 1)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sim", required=True)
    ap.add_argument("--programs", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--length", type=int, default=40)
    ap.add_argument("--simlen", type=int, default=400000)
    ap.add_argument("--no-fp", action="store_true")
    ap.add_argument("--stop-on-bug", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    r = campaign(Path(a.sim), a.programs, a.seed, a.length, a.simlen, not a.no_fp, a.stop_on_bug, log=sys.stdout)
    r["sim"] = a.sim
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True); Path(a.out).write_text(json.dumps(r, indent=1))
    print(json.dumps({k: v for k, v in r.items() if k != "first_detection"}),
          "| first:", (r["first_detection"] or {}).get("n_programs"))


if __name__ == "__main__":
    main()
