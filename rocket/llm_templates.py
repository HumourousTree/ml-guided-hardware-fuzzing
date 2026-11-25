#!/usr/bin/env python3
"""Step 2: LLM-written generator templates for Rocket/BOOM, from automatically measured ISA gaps only.

  python3 rocket/llm_templates.py [--rounds 3] [--per-round 12] [--model gemini-3.5-flash-lite] [--out rocket/templates/llm.json]

Each round: (1) ISA census of the current generator (rocket/isa_census.py: never-executed RV64GC instructions, exception
causes, CSRs); (2) one LLM request for templates that close those gaps; (3) static checks (placeholders, reserved
registers, forward-only labels, length); (4) dynamic validation: every template is exercised in --validate programs on
Spike and on the bug-free references of Rocket and BOOM, and is rejected if Spike fails or any bug-free core disagrees
with Spike or hangs (the LLM never sees the bugs, the hand-designed ext profile or the published results). Rejections and
the new census feed the next round. The LLM runs only here, on the user's machine; campaigns with the accepted templates
(profile "llm") run anywhere without a key.
"""
import argparse
import json
import random
import re
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rocket"))
import isa_census  # noqa: E402
import rv64fuzz  # noqa: E402

REFS = {"rocket": ["golden_driver_2f09f728", "golden_multiplexer"], "boom": ["golden_driver_83089d9a", "golden_multiplexer"]}
FORBIDDEN = [(re.compile(r"\b(x28|x29|x31|t3|t4|t6)\b"), "uses a register reserved by the harness (x28, x29, x31)"),
             (re.compile(r"\b(mtvec|tohost|fromhost|satp|pmpcfg\d*|pmpaddr\d*|medeleg|mideleg)\b"),
              "touches state the harness depends on (trap vector, host interface, memory protection)")]

PROMPT = """You are extending a random instruction generator for differential testing of 64-bit RISC-V cores (RV64GC,
machine mode) against the Spike ISA simulator. Programs are random sequences of about 40 instructions; at the end every
integer and FP register, fcsr, mscratch, mstatus, mepc, mcause, mtval, mie and a 256-byte data region are compared with Spike.

Harness facts (fixed; templates must respect them):
- x30 holds the base address of a 256-byte read-write data region; x28, x29 and x31 are reserved; never use them.
- A machine-mode trap handler is installed: on an exception it records the cause and resumes at the next 4-byte
  instruction. Instructions that may trap must therefore not be compressed.
- Do not write mtvec, satp, pmp*, medeleg/mideleg, and do not touch tohost/fromhost. Do not loop: branches and jumps
  may only go forward to a label defined later in the same template.

Measured gaps of the current generator ({programs} programs, decoded from Spike's execution log):
{census}

Write up to {k} NEW templates that make the generator exercise what is never executed, including instruction
combinations and corner cases likely to expose processor bugs (hazards between dependent instructions, unusual operand
values, exceptions). Each template is 1 to 10 assembly lines in GNU syntax with these placeholders:
  {{xA}} .. {{xZ}}   integer registers (the same letter = the same register within one template instance)
  {{fA}} .. {{fZ}}   FP registers
  {{imm:LO:HI}}      a random integer in [LO, HI]
  {{off:N}}          a random byte offset into the data region, aligned to N (1, 2, 4 or 8); use it as  {{off:8}}(x30)
                     or  addi {{xA}}, x30, {{off:8}}  to form an address
  {{label:A}}        a unique label; define it as  {{label:A}}:  on its own line, after its use
Set "rvc": true only for templates of compressed instructions (c.*) that cannot trap; otherwise false.
{rejected}
Reply with JSON only:
{{"templates": [{{"name": "...", "targets": ["instruction or behaviour", ...], "rvc": false, "asm": ["...", "..."]}}]}}"""


def census_text(c: dict) -> str:
    lines = []
    for g, v in c["groups"].items():
        lines.append(f"- {g}: {v['executed']}/{v['total']} instructions executed; never: {' '.join(v['never']) or 'none'}")
    lines.append(f"- exception causes seen: {', '.join(c['exception_causes']) or 'none'}")
    lines.append(f"- CSRs accessed: {', '.join(c['csrs_accessed']) or 'none'}")
    return "\n".join(lines)


def static_check(t: dict) -> str | None:
    if not isinstance(t, dict) or not isinstance(t.get("asm"), list) or not t["asm"] or len(t["asm"]) > 10:
        return "malformed (needs 1-10 asm lines)"
    text = "\n".join(map(str, t["asm"]))
    for m in re.finditer(r"\{[^}]*\}", text):
        if not rv64fuzz.TEMPLATE_RE.fullmatch(m.group(0)):
            return f"unknown placeholder {m.group(0)}"
    for rx, why in FORBIDDEN:
        if rx.search(text):
            return why
    for l in t["asm"]:                    # x30 (the data pointer) may be read, never written
        parts = re.split(r"[\s,]+", str(l).strip(), maxsplit=2)
        op = parts[0].lower() if parts else ""
        reads_first = (op.startswith(("s", "fs", "c.s", "c.fs", "b", "c.b", "j", "csrw", "csrs", "csrc", "fence"))
                       and not op.startswith(("sl", "sr", "su", "sext", "seqz", "snez", "sgt", "sc.")))
        if not reads_first and len(parts) > 1 and parts[1] in ("x30", "t5"):
            return "writes x30, the data-region pointer"
    for lab in set(re.findall(r"\{label:([A-Z])\}", text)):
        uses = [i for i, l in enumerate(t["asm"]) if f"{{label:{lab}}}" in l and not l.strip().endswith(":")]
        defs = [i for i, l in enumerate(t["asm"]) if l.strip() == f"{{label:{lab}}}:"]
        if len(defs) != 1 or not uses or min(uses) > defs[0]:
            return f"label {lab} must be defined once, after its use (forward branches only)"
    return None


def validate(t: dict, n: int) -> str | None:
    """Exercise one template on Spike and on the bug-free cores; return a rejection reason or None."""
    w = Path(tempfile.mkdtemp(prefix="tplval-"))
    for core, refs in REFS.items():
        rng = random.Random(1234)
        subst = rv64fuzz.QUIRKS[core].get("subst")
        for k in range(n):
            try:
                elf = rv64fuzz.build_elf(rv64fuzz.gen_program(rng, 40, True, "llm", subst, [t]), w)
            except Exception:
                return "does not assemble"
            try:
                exp = rv64fuzz.run_spike(elf)
            except Exception:
                return "Spike does not terminate (loop?)"
            if exp is None:
                return "Spike does not finish the program"
            for ref in refs:
                got, stopped = rv64fuzz.run_rocket(Path.home() / f"{core}_runs" / ref / "Vtop_tiny_soc", elf, 400000)
                if not stopped:
                    return f"the bug-free {core} hangs"
                if got != exp:
                    return (f"the bug-free {core} disagrees with Spike (implementation-defined or a known core "
                            f"difference)")
    return None


def ask(model: str, prompt: str) -> tuple[list, str]:
    from harness import pico_campaign as pc
    client, _ = pc._get_gemini_client()
    text, used, _ = pc._generate_with_gemini(client, [model], prompt, purpose="rocket_templates")
    if not text:
        return [], "no answer"
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0))["templates"], used
    except Exception:
        return [], "unparseable answer"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--per-round", type=int, default=12)
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    ap.add_argument("--census-programs", type=int, default=100)
    ap.add_argument("--validate", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "rocket" / "templates" / "llm.json"))
    a = ap.parse_args()
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    accepted, rejected, rounds, requests = [], [], [], 0
    before = isa_census.census("base", a.census_programs, 4242)
    cur = before
    for r in range(1, a.rounds + 1):
        rej_txt = ("\nPreviously rejected templates (do not repeat these mistakes):\n" +
                   "\n".join(f"- {x['name']}: {x['reason']}" for x in rejected[-15:]) + "\n") if rejected else ""
        prompt = PROMPT.format(programs=a.census_programs, census=census_text(cur), k=a.per_round, rejected=rej_txt)
        tpls, used = ask(a.model, prompt); requests += 1
        print(f"round {r}: {len(tpls)} templates from {used}", flush=True)
        fresh = []
        for i, t in enumerate(tpls):
            t = dict(t, name=f"r{r}_{i}_{re.sub(r'[^A-Za-z0-9_]', '_', str(t.get('name', 'tpl')))[:40]}")
            why = static_check(t)
            (rejected.append({"name": t["name"], "reason": why, "asm": t.get("asm")}) if why else fresh.append(t))
        with ProcessPoolExecutor(a.jobs) as ex:
            verdicts = list(ex.map(validate, fresh, [a.validate] * len(fresh)))
        for t, why in zip(fresh, verdicts):
            if why:
                rejected.append({"name": t["name"], "reason": why, "asm": t["asm"]})
            else:
                accepted.append(t)
        if accepted:     # census of base + all accepted templates so far
            cur = _census_with(accepted, a.census_programs)
        rounds.append({"round": r, "proposed": len(tpls), "accepted": sum(1 for t in accepted if t["name"].startswith(f"r{r}_")),
                       "census": {g: v["executed"] for g, v in cur["groups"].items()},
                       "exception_causes": list(cur["exception_causes"])})
        print(json.dumps(rounds[-1]), flush=True)
        out.write_text(json.dumps({"model": a.model, "requests": requests, "rounds": rounds, "census_before": before,
                                   "census_after": cur, "templates": accepted, "rejected": rejected}, indent=1))
    print(f"{len(accepted)} templates accepted, {len(rejected)} rejected, {requests} LLM requests -> {out}")


def _census_with(templates: list, programs: int) -> dict:
    orig = rv64fuzz.gen_program

    def patched(rng, length, fp=True, profile="base", subst=None, tpls=None):
        return orig(rng, length, fp, "llm", subst, templates)
    isa_census.rv64fuzz.gen_program = patched
    try:
        return isa_census.census("llm", programs, 4242)
    finally:
        isa_census.rv64fuzz.gen_program = orig


if __name__ == "__main__":
    main()
