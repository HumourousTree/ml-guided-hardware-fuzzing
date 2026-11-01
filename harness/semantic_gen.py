"""ChipFuzzer-style Stage-I test generation for BenchFuzz (target-line-driven, assembly-in, corrected-by-feedback).

Pipeline per target: pick an uncovered RTL line segment -> retrieve the most relevant seed as a template ->
prompt the LLM for RISC-V *assembly* -> assemble (syntax corrector: assembler diagnostics are fed back) ->
validate (legalisation feedback) -> run on the DUT -> if the target is still uncovered, semantic corrector
(feedback: which target lines are still missing + what the template covered) -> keep the program as a seed.

Simplifications versus ChipFuzzer (to be stated in the paper): similarity indexing uses *covered RTL line proximity*
instead of Pyverilog control-flow paths; the discrepancy analysis is a rule-based checklist plus the retrieved
template, not a second LLM pass; there is no Bug-Guided stage (see `bug_priors` for an analogue).
"""
import random
import re
from typing import Callable

from harness.asm import assemble, AsmError
from harness.delta_debugger import disassemble_rv32i
from harness.riscv_cosim import analyze_program

_KEY_RE = re.compile(r"(?:\x01|\\001)(\w+)(?:\x02|\\002)([^\x01\\]*)")


def decode_point(key: str) -> dict:
    """Verilator coverage-point key -> {'page','l',...}. Works on real keys and the test keys."""
    kv = {k: v for k, v in _KEY_RE.findall(key)}
    kv["kind"] = kv.get("page", "").split("/")[0]
    return kv


def uncovered_line_segments(universe: set[str], covered: set[str], max_gap: int = 2) -> list[tuple[int, int]]:
    """Contiguous (line_lo, line_hi) segments of v_line points that nothing has covered yet."""
    lines_all, lines_cov = set(), set()
    for k in universe:
        d = decode_point(k)
        if d["kind"] == "v_line" and d.get("l", "").isdigit():
            lines_all.add(int(d["l"]))
            if k in covered: lines_cov.add(int(d["l"]))
    miss = sorted(lines_all - lines_cov)
    segs, start, prev = [], None, None
    for l in miss:
        if start is None: start = prev = l
        elif l - prev <= max_gap: prev = l
        else: segs.append((start, prev)); start = prev = l
    if start is not None: segs.append((start, prev))
    return segs


def covered_lines(points: set[str]) -> set[int]:
    out = set()
    for k in points:
        d = decode_point(k)
        if d["kind"] == "v_line" and d.get("l", "").isdigit(): out.add(int(d["l"]))
    return out


class SeedDB:
    """Seeds with the RTL lines they cover; retrieval by proximity (simplified path-similarity indexing)."""

    def __init__(self):
        self.entries: list[tuple[dict, set[int]]] = []

    def add(self, program: dict, points: set[str]) -> None:
        self.entries.append((program, covered_lines(points)))

    def retrieve(self, seg: tuple[int, int], radius: int = 40) -> dict | None:
        if not self.entries: return None
        lo, hi = seg[0] - radius, seg[1] + radius
        def score(e):                       # more covered lines near the target = more similar; ties -> smaller coverage
            return (len([l for l in e[1] if lo <= l <= hi]), -len(e[1]))
        return max(self.entries, key=score)[0]


ISA_FACTS = """Target core: PicoRV32 (RV32IM only, no CSR/system/fence, no compressed). Allowed: add sub sll slt sltu xor srl sra or and,
addi slti sltiu xori ori andi slli srli srai, lb lh lw lbu lhu sb sh sw, beq bne blt bge bltu bgeu, jal jalr, lui auipc,
mul mulh mulhsu mulhu div divu rem remu, plus pseudo-ops li mv nop not neg seqz snez j beqz bnez. Registers x0-x31 or ABI names.
Legality: data addresses must be naturally aligned, inside 0x100..0xF000 and must not overlap the program image
(load a base register first, e.g. `li x31, 0x1000`); branches/jumps forward only; the program must fall through to its end."""


def build_prompt(rtl_lines: list[str], seg: tuple[int, int], template: dict | None, ctx: int = 8, note: str = "") -> str:
    lo, hi = max(1, seg[0] - ctx), min(len(rtl_lines), seg[1] + ctx)
    code = "\n".join(f"{n:5d}{'>' if seg[0] <= n <= seg[1] else ' '} {rtl_lines[n - 1]}" for n in range(lo, hi + 1))
    tmpl = "(none yet)"
    if template:
        tmpl = "\n".join(disassemble_rv32i(int(w, 0)) for w in template["words"][:40])
    return f"""You are an RTL verification engineer. Write a RISC-V assembly test that makes the marked lines ('>') of the RTL execute.

--- target RTL (lines {seg[0]}-{seg[1]} marked '>') ---
{code}

--- retrieved template (covers nearby logic) ---
{tmpl}

--- discrepancy checklist ---
1. Find the first condition guarding the marked lines and what makes it true (operand values, previous instruction, memory state).
2. State preparation: which registers / memory words must hold what before the trigger? Prepare them with li/sw.
3. Trigger event: which instruction (type, operands, sequence/hazard) activates the marked lines?
4. Legality: obey the constraints below.

{ISA_FACTS}
{note}
Answer with ONE ```asm fenced block containing only assembly (labels allowed, # comments allowed). End with `nop`."""


def extract_asm(text: str) -> str:
    m = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


class SemanticGenerator:
    """Generates programs aimed at uncovered RTL. `backend` must expose universe/run(); llm_fn(prompt)->str|None."""

    def __init__(self, rtl_text: str, backend, llm_fn: Callable[[str], str | None], rng: random.Random,
                 max_syntax_fix: int = 5, max_semantic_fix: int = 5, max_target_failures: int = 3):
        self.rtl_lines = rtl_text.splitlines()
        self.backend, self.llm, self.rng = backend, llm_fn, rng
        self.max_syntax, self.max_semantic, self.max_fail = max_syntax_fix, max_semantic_fix, max_target_failures
        self.seeds = SeedDB()
        self.fail_counts: dict[tuple[int, int], int] = {}
        self.stats = {"llm_calls": 0, "targets": 0, "target_hit": 0, "syntax_fixes": 0, "semantic_fixes": 0,
                      "illegal": 0, "gave_up": 0, "asm_errors": {}}

    def add_seed(self, program: dict, points: set[str]) -> None:
        self.seeds.add(program, points)

    def pick_target(self, covered: set[str]) -> tuple[int, int] | None:
        segs = [s for s in uncovered_line_segments(self.backend.universe, covered)
                if self.fail_counts.get(s, 0) < self.max_fail]
        if not segs: return None
        segs.sort(key=lambda s: -(s[1] - s[0] + 1))            # prefer semantically related contiguous segments
        top = [s for s in segs if (s[1] - s[0]) >= max(1, (segs[0][1] - segs[0][0]) // 2)] or segs
        return self.rng.choice(top[:8])

    def _call(self, prompt: str) -> str | None:
        self.stats["llm_calls"] += 1
        return self.llm(prompt)

    def generate(self, covered: set[str], pid: str) -> list[tuple[dict, object]]:
        """Run the generation loop for one target. Returns every (program, run_result) that was executed."""
        seg = self.pick_target(covered)
        if seg is None: return []
        self.stats["targets"] += 1
        template = self.seeds.retrieve(seg)
        prompt, executed, note = build_prompt(self.rtl_lines, seg, template), [], ""
        seg_keys = {k for k in self.backend.universe if decode_point(k)["kind"] == "v_line"
                    and decode_point(k).get("l", "").isdigit() and seg[0] <= int(decode_point(k)["l"]) <= seg[1]}
        syntax_left, semantic_left = self.max_syntax, self.max_semantic
        while True:
            text = self._call(prompt + note)
            if text is None: break
            try:
                words = assemble(extract_asm(text))
            except AsmError as e:
                self.stats["asm_errors"][e.msg.split("'")[0].strip()[:40]] = self.stats["asm_errors"].get(e.msg.split("'")[0].strip()[:40], 0) + 1
                if syntax_left <= 0: break
                syntax_left -= 1; self.stats["syntax_fixes"] += 1
                note = f"\n\nYour assembly failed to assemble: {e}\nFix it and answer again with the full corrected program."
                continue
            if not words or words[-1] != 0x13: words = words + [0x13]
            prog = {"id": f"{pid}-t{seg[0]}", "description": f"semantic target lines {seg[0]}-{seg[1]}",
                    "base": "0x00000000", "words": [f"0x{w:08x}" for w in words]}
            ana = analyze_program(words)
            if not ana["ok"]:
                self.stats["illegal"] += 1
                if syntax_left <= 0: break
                syntax_left -= 1; self.stats["syntax_fixes"] += 1
                note = f"\n\nYour program is not a legal test: {ana['reason']}.\nFix it and answer again."
                continue
            res = self.backend.run(prog)
            executed.append((prog, res))
            hit = bool(seg_keys & (res.covered | set()))
            newly = seg_keys & res.covered - covered
            if newly or not seg_keys:
                self.stats["target_hit"] += 1; self.seeds.add(prog, res.covered); return executed
            if res.covered: self.seeds.add(prog, res.covered)
            if semantic_left <= 0: break
            semantic_left -= 1; self.stats["semantic_fixes"] += 1
            missing = sorted(l for l in range(seg[0], seg[1] + 1))[:12]
            note = (f"\n\nThe program ran but did not execute the marked lines {missing}. Execution feedback: it covered "
                    f"{len(res.covered)} RTL points, none of them in the target. Revise the previous program (do not start over): "
                    f"re-check the guarding condition and the state preparation, then answer again.")
        self.fail_counts[seg] = self.fail_counts.get(seg, 0) + 1
        self.stats["gave_up"] += 1
        return executed
