#!/usr/bin/env python3
"""ISA census: which RV64GC instructions, CSRs and exception causes a generator profile actually executes (on Spike).

  python3 rocket/isa_census.py [--profile base] [--programs 200] [--seed 4242] [--core rocket] [--out census.json]

The instruction universe and encodings come from Spike's own sources (riscv/riscv.mk.in groups, riscv/encoding.h
MATCH/MASK and CSR numbers), so the census is exact: every executed instruction word is decoded from its bits (not from
the disassembly, which prints pseudo-instructions). This is the *only* feedback the LLM generation step (Step 2) gets
about the generator: automatically measured gaps, never the hand-designed ext profile.
"""
import argparse
import collections
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rv64fuzz  # noqa: E402

SPIKE_SRC = Path.home() / "ext" / "riscv-isa-sim" / "riscv"
GROUPS = {"I": "ext_i", "M": "ext_m", "A": "ext_a", "F": "ext_f", "D": "ext_d", "C": "ext_c", "priv": "priv"}
RV32_ONLY = {"c_jal", "c_flw", "c_fsw", "c_flwsp", "c_fswsp"}
NOT_APPLICABLE = {"dret", "sret", "sfence_vma", "wfi"}     # debug mode / S-mode / sleep: outside the M-mode test harness


def isa_tables():
    mk = (SPIKE_SRC / "riscv.mk.in").read_text()
    groups = {}
    for name, var in GROUPS.items():
        m = re.search(rf"^riscv_insn_{var} = \\\n((?:\t\S+ \\\n?|\t\S+\n)+)", mk, re.M)
        groups[name] = [w.strip(" \\\t") for w in m.group(1).split("\n") if w.strip(" \\\t")]
    enc = (SPIKE_SRC / "encoding.h").read_text()
    defs = dict(re.findall(r"#define (MATCH_\w+|MASK_\w+|CSR_\w+) (0x[0-9a-fA-F]+)", enc))
    insns = {}
    for g, names in groups.items():
        for n in names:
            if n in RV32_ONLY or n in NOT_APPLICABLE or n.endswith("_q") or "_q_" in n:   # (Q: not in RV64GC)
                continue
            k = n.upper()
            if f"MATCH_{k}" in defs:
                insns[n] = (g, int(defs[f"MATCH_{k}"], 16), int(defs[f"MASK_{k}"], 16))
    csrs = {int(v, 16): k[4:].lower() for k, v in defs.items() if k.startswith("CSR_")}
    return insns, csrs


def decode(word: int, width: int, insns) -> str | None:
    best, bits = None, -1
    for n, (_, match, mask) in insns.items():
        compressed = n.startswith("c_")
        if compressed != (width == 16):
            continue
        if word & mask == match and bin(mask).count("1") > bits:
            best, bits = n, bin(mask).count("1")
    return best


def census(profile: str, programs: int, seed: int, core: str = "rocket") -> dict:
    insns, csrs = isa_tables()
    subst = rv64fuzz.QUIRKS.get(core, {}).get("subst")
    rng = random.Random(seed)
    w = Path(tempfile.mkdtemp())
    seen, causes, csr_seen = collections.Counter(), collections.Counter(), collections.Counter()
    line = re.compile(r"core\s+0: 0x([0-9a-f]+) \((0x[0-9a-f]+)\)")
    for _ in range(programs):
        elf = rv64fuzz.build_elf(rv64fuzz.gen_program(rng, 40, True, profile, subst), w)
        p = subprocess.run(["spike", "--isa=rv64gc", "-m0x80000000:0x800000,0x60000000:0x1000", "-l", str(elf)],
                           env=rv64fuzz.ENV, capture_output=True, text=True, timeout=60)
        for l in p.stderr.splitlines():
            m = line.match(l)
            if m and int(m.group(1), 16) >= 0x80000000:          # our program only (not Spike's boot ROM)
                word = int(m.group(2), 16)
                width = 16 if word & 3 != 3 else 32
                n = decode(word, width, insns)
                if n:
                    seen[n] += 1
                    if n.startswith("csr"):
                        csr_seen[csrs.get(word >> 20, hex(word >> 20))] += 1
            elif "exception" in l:
                c = re.search(r"exception (\w+)", l)
                if c:
                    causes[c.group(1)] += 1
    by_group = collections.defaultdict(lambda: {"executed": [], "never": []})
    for n, (g, _, _) in sorted(insns.items()):
        by_group[g]["executed" if seen[n] else "never"].append(n.replace("_", "."))
    return {"profile": profile, "programs": programs, "seed": seed, "core": core,
            "groups": {g: {"executed": len(v["executed"]), "total": len(v["executed"]) + len(v["never"]),
                           "never": v["never"]} for g, v in sorted(by_group.items())},
            "exception_causes": dict(causes), "csrs_accessed": dict(csr_seen.most_common()),
            "instruction_counts": dict(seen.most_common())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="base")
    ap.add_argument("--programs", type=int, default=200)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--core", default="rocket")
    ap.add_argument("--out")
    a = ap.parse_args()
    r = census(a.profile, a.programs, a.seed, a.core)
    for g, v in r["groups"].items():
        print(f"{g:5s} {v['executed']:3d}/{v['total']:3d} executed; never: {' '.join(v['never'][:25])}"
              + (" ..." if len(v["never"]) > 25 else ""))
    print("exception causes:", r["exception_causes"] or "none")
    print("CSRs accessed:", list(r["csrs_accessed"]))
    if a.out:
        Path(a.out).write_text(json.dumps(r, indent=1))


if __name__ == "__main__":
    main()
