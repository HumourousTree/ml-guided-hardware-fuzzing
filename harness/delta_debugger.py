"""Delta-debugging, RTL fault localisation and patch plumbing.

Differences from the first prototype (see CHANGES.md for the full list):

* The register snapshot used by the reproducer is taken *after* instruction
  K-3 (K-2 steps).  The prototype took it one instruction too early.
* The reproducer is built from the *dynamic* ISS trace, not from static
  ``words[K-2:K+1]``.  Static indexing is wrong as soon as a taken branch or
  jump precedes the failure.  Memory words read by the window are pre-loaded.
* ``ddmin_reproducer`` really minimises the reproducer against the DUT.  The
  old "shrink schedule" only printed target sizes and never removed anything.
* ``localize_rtl_fault`` anchors on the real datapath statements (it used to
  match a decoder line for branch faults) and returns several candidate blocks.
* The LLM now returns *block patches* that are spliced into the RTL instead of
  being asked to regenerate a 3,000-line file from a 60-line excerpt.
"""

import re
from typing import Callable

from sw_reference.golden_rv32i import RV32IReference

NOP = 0x00000013


def _as_int(value) -> int:
    """Convert hex string or int to int."""
    return int(value, 0) if isinstance(value, str) else int(value)


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _s32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


# ── Disassembler ─────────────────────────────────────────────────────────────

def disassemble_rv32i(insn: int) -> str:
    """Lightweight RV32IM disassembler used for diagnostics and prompts."""
    insn = _u32(insn)
    opcode = insn & 0x7F
    rd = (insn >> 7) & 31
    funct3 = (insn >> 12) & 7
    rs1 = (insn >> 15) & 31
    rs2 = (insn >> 20) & 31
    funct7 = (insn >> 25) & 0x7F

    i_imm = (insn >> 20) if not (insn & 0x80000000) else (insn >> 20) - 4096
    s_imm = (((insn >> 25) << 5) | rd)
    if s_imm & 0x800:
        s_imm -= 4096
    b_imm = (((insn >> 31) << 12) | (((insn >> 7) & 1) << 11) | (((insn >> 25) & 0x3F) << 5) | (((insn >> 8) & 0xF) << 1))
    if b_imm & 0x1000:
        b_imm -= 8192
    u_imm = insn & 0xFFFFF000
    j_imm = (((insn >> 31) << 20) | (((insn >> 12) & 0xFF) << 12) | (((insn >> 20) & 1) << 11) | (((insn >> 21) & 0x3FF) << 1))
    if j_imm & 0x100000:
        j_imm -= 2097152

    if opcode == 0x37:
        return f"lui x{rd}, 0x{u_imm >> 12:x}"
    elif opcode == 0x17:
        return f"auipc x{rd}, 0x{u_imm >> 12:x}"
    elif opcode == 0x6F:
        return f"jal x{rd}, {j_imm}"
    elif opcode == 0x67 and funct3 == 0:
        return f"jalr x{rd}, {i_imm}(x{rs1})"
    elif opcode == 0x63:
        b_ops = {0: "beq", 1: "bne", 4: "blt", 5: "bge", 6: "bltu", 7: "bgeu"}
        return f"{b_ops.get(funct3, 'branch')} x{rs1}, x{rs2}, {b_imm}"
    elif opcode == 0x13:
        if insn == NOP:
            return "nop"
        shamt = (insn >> 20) & 31
        op_map = {
            0: f"addi x{rd}, x{rs1}, {i_imm}",
            1: f"slli x{rd}, x{rs1}, {shamt}",
            2: f"slti x{rd}, x{rs1}, {i_imm}",
            3: f"sltiu x{rd}, x{rs1}, {i_imm}",
            4: f"xori x{rd}, x{rs1}, {i_imm}",
            5: f"srai x{rd}, x{rs1}, {shamt}" if funct7 == 0x20 else f"srli x{rd}, x{rs1}, {shamt}",
            6: f"ori x{rd}, x{rs1}, {i_imm}",
            7: f"andi x{rd}, x{rs1}, {i_imm}",
        }
        return op_map.get(funct3, f"op-imm x{rd}, x{rs1}, {i_imm}")
    elif opcode == 0x33:
        if funct7 == 0x01:
            m_ops = {0: "mul", 1: "mulh", 2: "mulhsu", 3: "mulhu", 4: "div", 5: "divu", 6: "rem", 7: "remu"}
            return f"{m_ops.get(funct3, 'mul-div')} x{rd}, x{rs1}, x{rs2}"
        op_map = {
            0: f"sub x{rd}, x{rs1}, x{rs2}" if funct7 == 0x20 else f"add x{rd}, x{rs1}, x{rs2}",
            1: f"sll x{rd}, x{rs1}, x{rs2}",
            2: f"slt x{rd}, x{rs1}, x{rs2}",
            3: f"sltu x{rd}, x{rs1}, x{rs2}",
            4: f"xor x{rd}, x{rs1}, x{rs2}",
            5: f"sra x{rd}, x{rs1}, x{rs2}" if funct7 == 0x20 else f"srl x{rd}, x{rs1}, x{rs2}",
            6: f"or x{rd}, x{rs1}, x{rs2}",
            7: f"and x{rd}, x{rs1}, x{rs2}",
        }
        return op_map.get(funct3, f"op x{rd}, x{rs1}, x{rs2}")
    elif opcode == 0x03:
        l_ops = {0: "lb", 1: "lh", 2: "lw", 4: "lbu", 5: "lhu"}
        return f"{l_ops.get(funct3, 'load')} x{rd}, {i_imm}(x{rs1})"
    elif opcode == 0x23:
        s_ops = {0: "sb", 1: "sh", 2: "sw"}
        return f"{s_ops.get(funct3, 'store')} x{rs2}, {s_imm}(x{rs1})"

    return f"raw 0x{insn:08x}"


# ── Instruction generators ───────────────────────────────────────────────────

def _li_instruction(rd: int, value: int) -> int | tuple[int, int]:
    """Load-immediate: one ADDI for 12-bit values, otherwise LUI+ADDI."""
    value = _u32(value)
    if -2048 <= _s32(value) <= 2047:
        imm = value & 0xFFF
        return (imm << 20) | (0 << 15) | (0 << 12) | (rd << 7) | 0x13

    upper = (value >> 12) & 0xFFFFF
    lower = value & 0xFFF
    if lower > 0x7FF:
        upper = (upper + 1) & 0xFFFFF
    lower_se = lower if lower <= 0x7FF else lower - 0x1000

    lui = (upper << 12) | (rd << 7) | 0x37
    addi = ((lower_se & 0xFFF) << 20) | (rd << 15) | (0 << 12) | (rd << 7) | 0x13
    return (lui, addi)


def _li_words(rd: int, value: int) -> list[int]:
    res = _li_instruction(rd, value)
    return list(res) if isinstance(res, tuple) else [res]


def _sw(rs2: int, rs1: int, imm: int) -> int:
    imm &= 0xFFF
    return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((imm & 0x1F) << 7) | 0x23


def _generate_li_preamble(target_state: dict, base_addr: int = 0x0, nonzero_only: bool = True,
                          force_regs: tuple[int, ...] = ()) -> list[int]:
    """LI preamble restoring x1..x31 (at most 62 instructions: LUI+ADDI per register).

    ``force_regs`` are written even when their target value is zero (needed
    when scratch registers were clobbered by a memory pre-load).
    """
    instructions: list[int] = []
    for reg_num in range(1, 32):
        value = _as_int(target_state.get(f"x{reg_num}", 0))
        if nonzero_only and value == 0 and reg_num not in force_regs:
            continue
        instructions.extend(_li_words(reg_num, value))
    return instructions


# ── Pinpoint and slice ───────────────────────────────────────────────────────

def find_first_mismatch_index(comparison_result: dict) -> int | None:
    """Retirement index K of the first mismatch (0-based), or None."""
    mismatches = comparison_result.get("mismatches", [])
    if not mismatches:
        return None
    return mismatches[0].get("retire_order", 0)


def extract_register_state_at_instruction(program: dict, num_steps: int) -> dict:
    """Run the ISS for ``num_steps`` retirements and return x0..x31 *after* them."""
    words = [_as_int(w) for w in program.get("words", [])]
    base = _as_int(program.get("base", 0))
    reference = RV32IReference(words, base)
    for _ in range(max(0, num_steps)):
        try:
            reference.step()
        except (ValueError, KeyError):
            break
    return {f"x{i}": _u32(reference.regs[i]) for i in range(32)}


def _words_to_hex(words: list[int]) -> list[str]:
    return [f"0x{w & 0xFFFFFFFF:08x}" for w in words]


def _prefix_fallback(program: dict, words: list[int], base: int, K: int,
                     validator: Callable[[dict], bool] | None) -> tuple[dict, str]:
    """Static prefix covering every instruction executed up to and including K."""
    ref = RV32IReference(words, base)
    max_idx = 0
    for _ in range(K + 1):
        idx = (ref.pc - base) // 4
        max_idx = max(max_idx, idx)
        try:
            ref.step()
        except ValueError:
            break
    selected = words[:min(len(words), max_idx + 1)]
    if not selected or selected[-1] != NOP:
        selected.append(NOP)
    cand = {
        "id": f"{program.get('id', 'prog')}-prefix-k{K}",
        "description": f"Static prefix through the instruction executed at retirement {K}",
        "base": program.get("base", "0x00000000"),
        "words": _words_to_hex(selected),
    }
    if validator is None or validator(cand):
        return cand, "prefix_fallback"
    return program, "original"


def slice_to_minimal_reproducer(
    program: dict,
    comparison_result: dict,
    keep_hazard_context: bool = True,
    validator: Callable[[dict], bool] | None = None,
) -> tuple[dict, int | None, dict]:
    """Slice a failing program to ``preamble + window + NOP``.

    1. K = first mismatching retirement.
    2. Run the ISS: ``s = K-2`` (hazard context) and keep the *dynamic*
       instructions retired at steps s..K-1 plus the failing instruction K.
       If they are not a straight-line sequence (a taken branch/jump occurred)
       a static prefix is returned instead.
    3. Pre-load architectural state as it was *before step s* (= after K-3):
       memory words read by the window, then x1..x31.
    4. ``validator`` (e.g. ``riscv_cosim.validate_program``) must accept the
       result, otherwise fall back to the prefix.

    Returns ``(reproducer, K, register_state_before_window)``.
    """
    words = [_as_int(w) for w in program.get("words", [])]
    base = _as_int(program.get("base", 0))

    K = find_first_mismatch_index(comparison_result)
    if K is None:
        return program, None, {}

    s = max(0, K - 2) if keep_hazard_context else K

    ref = RV32IReference(words, base)
    events: list[dict] = []
    ref_s = None
    ok = True
    for i in range(K):
        if i == s:
            ref_s = ref.clone()
        try:
            events.append(ref.step())
        except ValueError:
            ok = False
            break
    if not ok:
        reproducer, mode = _prefix_fallback(program, words, base, K, validator)
        reproducer["metadata"] = {"mode": mode, "failure_index_k": K}
        return reproducer, K, {}
    if ref_s is None:
        ref_s = ref.clone()

    fail_pc = ref.pc
    fail_insn = ref.peek_insn()
    pcs = [ev["pc_rdata"] for ev in events[s:K]] + [fail_pc]
    window = [ev["insn"] for ev in events[s:K]] + [fail_insn]
    state_before = {f"x{i}": _u32(ref_s.regs[i]) for i in range(32)}

    straight = all(b - a == 4 for a, b in zip(pcs, pcs[1:]))
    if not straight:
        reproducer, mode = _prefix_fallback(program, words, base, K, validator)
        reproducer["metadata"] = {"mode": mode, "failure_index_k": K, "reason": "non-sequential window"}
        return reproducer, K, state_before

    # Memory words the window will read.
    mem_words: dict[int, int] = {}
    probe = ref_s.clone()
    for _ in range(len(window)):
        try:
            ev = probe.step()
        except ValueError:
            break
        if ev["mem_rmask"]:
            addr = ev["mem_addr"] & ~3
            val = ref_s._load_raw(addr, 4)
            if val:
                mem_words[addr] = val

    mem_pre: list[int] = []
    for addr, val in sorted(mem_words.items()):
        mem_pre += _li_words(1, addr) + _li_words(2, val) + [_sw(2, 1, 0)]
    reg_pre = _generate_li_preamble(state_before, base, nonzero_only=True,
                                    force_regs=(1, 2) if mem_pre else ())

    combined = mem_pre + reg_pre + window + [NOP]
    failing_disasm = disassemble_rv32i(fail_insn)
    reproducer = {
        "id": f"{program.get('id', 'prog')}-delta-debug-k{K}",
        "description": (f"Minimal reproducer for retirement {K} ({failing_disasm}): "
                        f"{len(mem_pre)} memory + {len(reg_pre)} register preload instrs + "
                        f"{len(window) - 1} hazard-context instrs + target"),
        "base": program.get("base", "0x00000000"),
        "words": _words_to_hex(combined),
        "metadata": {
            "mode": "state_preload",
            "original_program_id": program.get("id"),
            "failure_index_k": K,
            "failing_instruction_hex": f"0x{fail_insn:08x}",
            "failing_instruction_disasm": failing_disasm,
            "state_preload_steps": s,
            "memory_preamble_length": len(mem_pre),
            "register_preamble_length": len(reg_pre),
            "hazard_context_length": len(window) - 1,
            "total_sliced_length": len(combined),
        },
    }
    if validator is not None and not validator(reproducer):
        fb, mode = _prefix_fallback(program, words, base, K, validator)
        fb["metadata"] = {"mode": mode, "failure_index_k": K,
                          "reason": "state-preload reproducer failed validation"}
        return fb, K, state_before
    return reproducer, K, state_before


def ddmin_reproducer(program: dict, fails: Callable[[dict], bool], protect_tail: int = 2,
                     max_tests: int = 300) -> tuple[dict, list[dict]]:
    """Classic ddmin (complement testing) over the head of a reproducer.

    ``fails(candidate)`` must return True iff the candidate is a *valid*
    program that still exposes the failure on the DUT.  The last
    ``protect_tail`` words (failing instruction + terminating NOP) are never
    removed.  Returns the minimised program and a log of every test performed.
    """
    words = [_as_int(w) for w in program.get("words", [])]
    if len(words) <= protect_tail:
        return program, []
    head, tail = words[:-protect_tail], words[-protect_tail:]

    def mk(h: list[int], tag: str) -> dict:
        return {
            "id": f"{program.get('id', 'prog')}-dd{tag}",
            "description": program.get("description", ""),
            "base": program.get("base", "0x00000000"),
            "words": _words_to_hex(h + tail),
            "metadata": dict(program.get("metadata", {})),
        }

    log: list[dict] = []
    cur = list(head)
    n = 2
    tests = 0
    while cur and tests < max_tests:
        chunk = -(-len(cur) // n)
        reduced = False
        for start in range(0, len(cur), chunk):
            cand = cur[:start] + cur[start + chunk:]
            tests += 1
            kept = bool(fails(mk(cand, str(tests))))
            log.append({"test": tests, "removed_words": min(chunk, len(cur) - start),
                        "remaining_words": len(cand) + len(tail), "still_fails": kept})
            if kept:
                cur = cand
                n = max(n - 1, 2)
                reduced = True
                break
            if tests >= max_tests:
                break
        if not reduced:
            if chunk == 1:
                break
            n = min(len(cur), n * 2)
    final = mk(cur, "-min")
    final["metadata"]["ddmin_tests"] = tests
    final["metadata"]["total_sliced_length"] = len(cur) + len(tail)
    return final, log


def prefix_program(program: dict, failure_index: int, percent: int) -> dict:
    """Valid prefix used to grow a repair back toward its original test set."""
    words = [_as_int(word) for word in program.get("words", [])]
    required = min(len(words), max(failure_index + 1, 1))
    target = max(required, (len(words) * percent + 99) // 100)
    selected = words[:min(len(words), target)]
    if selected and selected[-1] != NOP:
        selected.append(NOP)
    return {
        "id": f"{program.get('id', 'prog')}-grow-{percent}pct",
        "description": f"{percent}% regression verification prefix after delta-debug repair",
        "base": program.get("base", "0x00000000"),
        "words": _words_to_hex(selected),
        "metadata": {"growth_percent": percent, "failure_index_k": failure_index},
    }


class AdaptiveVectorScaler:
    """Grow schedule used to re-verify a repair on progressively longer prefixes.

    (The old ``SHRINK_SCHEDULE`` is kept only for API compatibility; real
    shrinking is performed by :func:`ddmin_reproducer`.)
    """

    SHRINK_SCHEDULE = [80, 60, 40, 20]
    GROW_SCHEDULE = [40, 60, 80, 100]

    def __init__(self, initial_size: int = 100):
        self.initial_size = initial_size
        self.current_size = initial_size
        self.shrink_phase_index = 0
        self.grow_phase_index = 0
        self.phase = "initial"

    def start_shrink(self):
        self.phase = "shrink"
        self.shrink_phase_index = 0

    def next_shrink_size(self) -> int | None:
        if self.shrink_phase_index >= len(self.SHRINK_SCHEDULE):
            return None
        pct = self.SHRINK_SCHEDULE[self.shrink_phase_index]
        self.shrink_phase_index += 1
        self.current_size = max(5, (self.initial_size * pct) // 100)
        return self.current_size

    def start_grow(self):
        self.phase = "grow"
        self.grow_phase_index = 0

    def next_grow_size(self) -> int | None:
        if self.grow_phase_index >= len(self.GROW_SCHEDULE):
            return None
        pct = self.GROW_SCHEDULE[self.grow_phase_index]
        self.grow_phase_index += 1
        self.current_size = pct
        return pct


# ── RTL fault localisation ───────────────────────────────────────────────────

def _find(lines: list[str], pred: Callable[[str], bool]) -> int | None:
    for idx, line in enumerate(lines):
        if pred(line):
            return idx
    return None


def _candidate(lines: list[str], subsystem: str, diagnosis: str, start: int, end: int) -> dict:
    total = len(lines)
    start = max(1, start)
    end = min(total, max(start, end))
    snippet_lines = lines[start - 1:end]
    return {
        "subsystem": subsystem,
        "start_line": start,
        "end_line": end,
        "total_snippet_lines": len(snippet_lines),
        "diagnosis": diagnosis,
        "snippet": "\n".join(snippet_lines),
        "numbered_snippet": "\n".join(f"{start + i:4d}: {l}" for i, l in enumerate(snippet_lines)),
    }


def _anchored(lines: list[str], subsystem: str, diagnosis: str, pred: Callable[[str], bool],
              before: int, after: int) -> dict | None:
    idx = _find(lines, pred)
    if idx is None:
        return None
    return _candidate(lines, subsystem, diagnosis, idx + 1 - before, idx + 1 + after)


def _module_block(lines: list[str], module: str, subsystem: str, diagnosis: str,
                  max_len: int = 160) -> dict | None:
    idx = _find(lines, lambda l: re.match(rf"\s*module\s+{module}\b", l) is not None)
    if idx is None:
        return None
    end = idx
    for j in range(idx, min(idx + max_len, len(lines))):
        if "endmodule" in lines[j]:
            end = j
            break
    else:
        end = min(idx + max_len, len(lines)) - 1
    return _candidate(lines, subsystem, diagnosis, idx + 1, end + 1)


def localize_rtl_fault(rtl_content: str, failure_record: dict) -> dict:
    """Return the most suspicious RTL blocks for an RVFI failure signature.

    The first entry of ``candidates`` is the primary block and its fields are
    mirrored at the top level for backward compatibility.  Several candidates
    are returned because an ``rd_wdata`` mismatch alone cannot distinguish an
    ALU fault from a write-back fault.
    """
    lines = rtl_content.splitlines()
    actual = failure_record.get("actual") or {}
    expected = failure_record.get("expected") or {}
    insn_raw = _u32(_as_int(actual.get("insn") or expected.get("insn") or 0))
    opcode = insn_raw & 0x7F
    funct3 = (insn_raw >> 12) & 7
    funct7 = (insn_raw >> 25) & 0x7F
    diffs = failure_record.get("differences") or {}

    alu_pred = lambda l: "alu_lts = " in l and "<=" not in l
    cands: list[dict | None] = []

    def alu(sub="alu", diag="ALU operator / comparator / bitwise mux divergence."):
        return _anchored(lines, sub, diag, alu_pred, 14, 46) or \
            _anchored(lines, sub, diag, lambda l: "alu_out = 'bx" in l, 24, 30)

    def regfile():
        return _anchored(lines, "regfile",
                         "Register write-back path (cpuregs_write / cpuregs_wrdata / latched_rd).",
                         lambda l: "cpuregs_wrdata = 'bx" in l, 6, 52)

    def memory():
        return _anchored(lines, "memory",
                         "Memory look-ahead interface: address, strobes, write data, read-data alignment.",
                         lambda l: "assign mem_la_read" in l, 6, 62)

    def next_pc():
        return _anchored(lines, "next_pc",
                         "Next-PC selection and link-register write in the fetch state.",
                         lambda l: "current_pc = reg_next_pc;" in l, 5, 55)

    is_m = opcode == 0x33 and funct7 == 0x01
    if is_m:
        if funct3 >= 4:
            cands.append(_module_block(lines, "picorv32_pcpi_div", "divider",
                                       "Divider / remainder datapath (sign handling, quotient, divide-by-zero)."))
        else:
            cands.append(_module_block(lines, "picorv32_pcpi_fast_mul", "multiplier",
                                       "Multiplier datapath (operand sign handling, product, high/low select)."))
    elif opcode == 0x63:
        cands += [alu("branch", "Branch condition (alu_eq/alu_lts/alu_ltu and alu_out_0 selection)."), next_pc()]
    elif opcode in (0x6F, 0x67):
        cands += [next_pc(), regfile()]
    elif opcode in (0x03, 0x23):
        cands.append(memory())
        marker = "cpu_state_ldmem: begin" if opcode == 0x03 else "cpu_state_stmem: begin"
        cands.append(_anchored(lines, "memory_fsm", "Load/store state-machine sequencing.",
                               lambda l, m=marker: m in l, 1, 36))
        cands.append(regfile())
    elif opcode in (0x13, 0x33, 0x37, 0x17):
        is_shift = (opcode == 0x13 and funct3 in (1, 5)) or (opcode == 0x33 and funct3 in (1, 5))
        if is_shift:
            cands.append(_anchored(lines, "shift", "Serial shifter state (cpu_state_shift).",
                                   lambda l: "cpu_state_shift: begin" in l, 1, 32))
        cands += [alu(), regfile()]

    if any(f in diffs for f in ("mem_addr", "mem_wdata", "mem_wmask", "mem_rmask", "mem_rdata")):
        cands.append(memory())
    if "pc_wdata" in diffs:
        cands.append(next_pc())

    seen: set[tuple[int, int]] = set()
    uniq: list[dict] = []
    for c in cands:
        if c and (c["start_line"], c["end_line"]) not in seen:
            seen.add((c["start_line"], c["end_line"]))
            uniq.append(c)
    if not uniq:
        fb = alu() or _candidate(lines, "unknown", "No anchor found; first 80 lines.", 1, 80)
        uniq.append(fb)

    primary = dict(uniq[0])
    primary["candidates"] = uniq
    return primary


# ── Patch protocol ───────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.S)
_PATCH_RE = re.compile(r"//\s*REPLACE\s+LINES\s+(\d+)\s*-\s*(\d+)[^\n]*\n(.*?)(?=//\s*REPLACE\s+LINES|\Z)", re.S)
_LINENO_RE = re.compile(r"^\s*\d+:\s?")


def _strip_line_numbers(code: str) -> str:
    rows = code.splitlines()
    non_blank = [r for r in rows if r.strip()]
    if non_blank and all(_LINENO_RE.match(r) for r in non_blank):
        return "\n".join(_LINENO_RE.sub("", r, count=1) for r in rows)
    return code


def parse_block_patches(text: str, default_range: tuple[int, int] | None = None) -> list[tuple[int, int, str]]:
    """Parse ``// REPLACE LINES a-b`` sections from an LLM answer."""
    fenced = _FENCE_RE.findall(text)
    body = "\n".join(fenced) if fenced else text
    patches = [(int(a), int(b), _strip_line_numbers(code).strip("\n"))
               for a, b, code in _PATCH_RE.findall(body)]
    if not patches and default_range and body.strip():
        patches = [(default_range[0], default_range[1], _strip_line_numbers(body).strip("\n"))]
    return patches


def apply_block_patches(rtl: str, patches: list[tuple[int, int, str]],
                        allowed: list[tuple[int, int]]) -> str:
    """Splice patches into ``rtl``; every patch must lie inside an offered block."""
    lines = rtl.splitlines()
    if not patches:
        raise ValueError("no patches found in model response")
    ordered = sorted(patches, key=lambda p: p[0])
    for (a, b, code), nxt in zip(ordered, ordered[1:] + [None]):
        if not (1 <= a <= b <= len(lines)):
            raise ValueError(f"patch range {a}-{b} outside file")
        if not any(lo <= a and b <= hi for lo, hi in allowed):
            raise ValueError(f"patch range {a}-{b} is outside the blocks offered to the model")
        if not code.strip():
            raise ValueError(f"empty replacement for lines {a}-{b}")
        if nxt is not None and nxt[0] <= b:
            raise ValueError("overlapping patches")
    for a, b, code in sorted(patches, key=lambda p: p[0], reverse=True):
        lines[a - 1:b] = code.splitlines()
    return "\n".join(lines) + "\n"


def format_localized_repair_prompt(
    minimal_reproducer: dict,
    failure_record: dict,
    localization: dict,
    golden_register_state: dict | None = None,
) -> str:
    """Prompt asking for block patches (not a whole-file rewrite)."""
    insn_disasm = failure_record.get("insn_disasm", "")
    insn_hex = failure_record.get("insn_hex", "")
    diffs = failure_record.get("differences", {}) or {}

    def fmt(v):
        return f"0x{v:08x} ({v})" if isinstance(v, int) else str(v)

    diff_str = "\n".join(
        f"  - {field}: expected (golden ISS) = {fmt(d.get('expected'))}, actual (RTL) = {fmt(d.get('actual'))}"
        for field, d in diffs.items()
    ) or f"  - {failure_record.get('error', 'no field-level difference recorded')}"

    preload_str = ""
    if golden_register_state:
        nonzero = {k: v for k, v in golden_register_state.items() if v != 0}
        if nonzero:
            preload_str = "\nArchitectural state before the hazard window (non-zero registers, first 8):\n" + "\n".join(
                f"  {reg}: 0x{val:08x} ({val})" for reg, val in list(nonzero.items())[:8])

    cands = localization.get("candidates") or [localization]
    blocks = "\n\n".join(
        f"--- Candidate block {i + 1}: {c['subsystem'].upper()} (lines {c['start_line']}-{c['end_line']}) ---\n"
        f"{c['diagnosis']}\n```verilog\n{c['numbered_snippet']}\n```"
        for i, c in enumerate(cands))
    prog_lines = "\n".join(f"  {w}" + (f"   # {disassemble_rv32i(_as_int(w))}" if True else "")
                           for w in minimal_reproducer.get("words", [])[-8:])

    return f"""You are a RISC-V hardware verification and RTL repair engineer.
The PicoRV32 RTL disagrees with the golden Python RV32IM reference. The reference is authoritative.

=== FAILING INSTRUCTION ===
{insn_disasm} ({insn_hex}) at retirement index K={failure_record.get('retire_order')}
Reproducer: {minimal_reproducer.get('id')} ({len(minimal_reproducer.get('words', []))} instructions; last 8 shown)
{prog_lines}

=== DISCREPANCY SIGNATURE ===
{diff_str}
{preload_str}

=== CANDIDATE RTL BLOCKS (the fault is most likely inside one of them) ===
{blocks}

=== TASK ===
Find the defect and fix it. Reply with ONE ```verilog fenced block containing, for each block you change,
a header line followed by the complete replacement text for those lines:

// REPLACE LINES <first>-<last>
<replacement code for exactly those lines>

Rules: only reference line ranges inside the candidate blocks above; keep every module name, port and
parameter unchanged; do not include line numbers in the code; do not add commentary outside the fenced block.
"""
