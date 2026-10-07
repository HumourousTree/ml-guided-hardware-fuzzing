"""Adaptive Delta-Debugging Protocol for instruction slicing, state pre-loading, and root-cause isolation."""

import json
from pathlib import Path
from sw_reference.golden_rv32i import RV32IReference


def _as_int(value) -> int:
    """Convert hex string or int to int32."""
    return int(value, 0) if isinstance(value, str) else int(value)


def _u32(value: int) -> int:
    """Mask to 32 bits."""
    return value & 0xFFFFFFFF


def _s32(value: int) -> int:
    """Signed 32-bit integer."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def disassemble_rv32i(insn: int) -> str:
    """Lightweight disassembler for RV32I instructions to assist diagnostics."""
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
        if insn == 0x00000013:
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


# ── Instruction Generators ──────────────────────────────────────────────────

def _li_instruction(rd: int, value: int) -> int | tuple[int, int]:
    """Generate Load Immediate using ADDI and LUI for a full 32-bit value."""
    value = _u32(value)
    if -2048 <= _s32(value) <= 2047:
        imm = value & 0xFFF
        return ((imm & 0xFFF) << 20) | (0 << 15) | (0 << 12) | (rd << 7) | 0x13

    upper = (value >> 12) & 0xFFFFF
    lower = value & 0xFFF
    if lower > 0x7FF:
        upper = (upper + 1) & 0xFFFFF
    lower_se = lower if lower <= 0x7FF else lower - 0x1000

    lui = (upper << 12) | (rd << 7) | 0x37
    addi = ((lower_se & 0xFFF) << 20) | (rd << 15) | (0 << 12) | (rd << 7) | 0x13
    return (lui, addi)


def _generate_li_preamble(target_state: dict, base_addr: int = 0x0, nonzero_only: bool = True) -> list[int]:
    """
    Generate a setup preamble of LI instructions to pre-load register state.
    target_state: dict mapping register name 'x1' through 'x31' to their values.
    Returns: list of instruction words (31 instructions or less).
    """
    instructions = []
    for reg_num in range(1, 32):
        reg_name = f"x{reg_num}"
        value = _as_int(target_state.get(reg_name, 0))
        if nonzero_only and value == 0:
            continue
        li_res = _li_instruction(reg_num, value)
        if isinstance(li_res, tuple):
            instructions.extend(li_res)
        else:
            instructions.append(li_res)

    return instructions


# ── Pinpoint and Slice ───────────────────────────────────────────────────────

def find_first_mismatch_index(comparison_result: dict) -> int | None:
    """
    Step 1: Identify the exact instruction retirement order K where first mismatch occurs.
    Returns K (0-indexed), or None if no mismatch.
    """
    mismatches = comparison_result.get("mismatches", [])
    if not mismatches:
        return None
    first = mismatches[0]
    return first.get("retire_order", 0)


def extract_register_state_at_instruction(program: dict, instruction_index: int) -> dict:
    """
    Step 3: Run the Python ISS up to instruction_index and return exact ground-truth register state.
    Returns: dict mapping 'x0' through 'x31' to their values.
    """
    words = [_as_int(w) for w in program.get("words", [])]
    base = _as_int(program.get("base", 0))

    reference = RV32IReference(words, base)
    # Run exactly instruction_index steps so we capture the state *before*
    # the instruction at instruction_index executes (pre-execution snapshot).
    for _ in range(max(0, instruction_index)):
        try:
            reference.step()
        except (ValueError, KeyError):
            break

    state = {f"x{i}": _u32(reference.regs[i]) for i in range(32)}
    return state


def slice_to_minimal_reproducer(
    program: dict,
    comparison_result: dict,
    keep_hazard_context: bool = True,
) -> tuple[dict, int | None, dict]:
    """
    Adaptive Delta-Debugging Slicing Protocol:
    Step 1: Pinpoint first failure index K
    Step 2: Discard instructions K+1 up to N
    Step 3: Pre-load architectural register state x1-x31 from Python ISS at index K-3
    Step 4: Keep instructions K-2 and K-1 in exact original form right before K
    
    Returns:
        (minimal_reproducer_dict, first_failure_index_K, extracted_state_at_K_minus_3)
    """
    words = [_as_int(w) for w in program.get("words", [])]
    base = _as_int(program.get("base", 0))

    K = find_first_mismatch_index(comparison_result)
    if K is None or K >= len(words):
        return program, None, {}

    # Step 3: Extract register state at K-3
    state_index = K - 3
    target_state = extract_register_state_at_instruction(program, state_index)

    # Step 3: Generate LI preamble
    preamble_words = _generate_li_preamble(target_state, base, nonzero_only=True)

    # Step 4: Preserve hazard context (K-2, K-1) and target instruction K
    hazard_start = max(0, K - 2) if keep_hazard_context else K
    hazard_and_target_words = words[hazard_start:K + 1]

    # Combined sequence: Preamble + Hazard Context + Target Instruction K + NOP
    combined_words = list(preamble_words) + list(hazard_and_target_words)
    if combined_words and combined_words[-1] != 0x00000013:
        combined_words.append(0x00000013)

    failing_insn_raw = words[K] if K < len(words) else 0
    failing_disasm = disassemble_rv32i(failing_insn_raw)

    minimal_reproducer = {
        "id": f"{program.get('id', 'prog')}-delta-debug-k{K}",
        "description": f"Minimal reproducer for instruction {K} ({failing_disasm}): preamble ({len(preamble_words)} instrs) + hazard context + target instruction {K}",
        "base": program.get("base", "0x00000000"),
        "words": [f"0x{w:08x}" for w in combined_words],
        "metadata": {
            "original_program_id": program.get("id"),
            "failure_index_k": K,
            "failing_instruction_hex": f"0x{failing_insn_raw:08x}",
            "failing_instruction_disasm": failing_disasm,
            "state_preload_index": state_index,
            "preamble_length": len(preamble_words),
            "hazard_context_length": len(hazard_and_target_words) - 1,
            "total_sliced_length": len(combined_words),
        },
    }

    return minimal_reproducer, K, target_state


def prefix_program(program: dict, failure_index: int, percent: int) -> dict:
    """Return a valid prefix used to grow a repair back toward its original test set."""
    words = [_as_int(word) for word in program.get("words", [])]
    required = min(len(words), max(failure_index + 1, 1))
    target = max(required, (len(words) * percent + 99) // 100)
    selected = words[:min(len(words), target)]
    if selected and selected[-1] != 0x00000013:
        selected.append(0x00000013)
    return {
        "id": f"{program.get('id', 'prog')}-grow-{percent}pct",
        "description": f"{percent}% regression verification prefix after delta-debug repair",
        "base": program.get("base", "0x00000000"),
        "words": [f"0x{word:08x}" for word in selected],
        "metadata": {"growth_percent": percent, "failure_index_k": failure_index},
    }


# ── Vector Shrink-Grow Scaling ──────────────────────────────────────────────

class AdaptiveVectorScaler:
    """Manages shrink and grow phases for adaptive test set sizing."""

    SHRINK_SCHEDULE = [80, 60, 40, 20]  # Percentage thresholds
    GROW_SCHEDULE = [40, 60, 80, 100]   # Percentage thresholds

    def __init__(self, initial_size: int = 100):
        self.initial_size = initial_size
        self.current_size = initial_size
        self.shrink_phase_index = 0
        self.grow_phase_index = 0
        self.phase = "initial"

    def start_shrink(self):
        """Begin shrinking phase."""
        self.phase = "shrink"
        self.shrink_phase_index = 0

    def next_shrink_size(self) -> int | None:
        """Return next target size in shrink phase, or None when minimal reached."""
        if self.shrink_phase_index >= len(self.SHRINK_SCHEDULE):
            return None
        pct = self.SHRINK_SCHEDULE[self.shrink_phase_index]
        self.shrink_phase_index += 1
        self.current_size = max(5, (self.initial_size * pct) // 100)
        return self.current_size

    def start_grow(self):
        """Begin growing phase after repair candidate passes minimal testcase."""
        self.phase = "grow"
        self.grow_phase_index = 0

    def next_grow_size(self) -> int | None:
        """Return next target size in grow phase, or None when 100% reached."""
        if self.grow_phase_index >= len(self.GROW_SCHEDULE):
            return None
        pct = self.GROW_SCHEDULE[self.grow_phase_index]
        self.grow_phase_index += 1
        self.current_size = pct
        return pct


# ── Localised RTL Fault Debugger (Section 11.3) ───────────────────────────────

def localize_rtl_fault(rtl_content: str, failure_record: dict) -> dict:
    """
    Slices the 3,000-line monolithic RTL down to a localized 30-80 line functional block
    responsible for the observed RVFI failure signature (Section 11.3).
    """
    lines = rtl_content.splitlines()
    total_lines = len(lines)

    insn_disasm = failure_record.get("insn_disasm", "").lower()
    insn_raw = _as_int(failure_record.get("actual", {}).get("insn", 0))
    opcode = _u32(insn_raw) & 0x7F
    funct7 = (_u32(insn_raw) >> 25) & 0x7F
    diffs = failure_record.get("differences", {})

    subsystem = "unknown"
    start_line = 1
    end_line = min(80, total_lines)
    diagnosis = "General hardware divergence."

    # Subsystem 1: Multiplier
    if (opcode == 0x33 and funct7 == 0x01) or any(m in insn_disasm for m in ("mul", "div", "rem")):
        subsystem = "multiplier"
        diagnosis = "Discrepancy in PCPI arithmetic multiplier/divider unit. Check product calculation and sign handling."
        for idx, line in enumerate(lines):
            if "module picorv32_pcpi_fast_mul" in line:
                start_line = idx + 1
                for jdx in range(idx, min(idx + 120, total_lines)):
                    if "endmodule" in lines[jdx]:
                        end_line = jdx + 1
                        break
                break

    # Subsystem 2: Branch / Jump / Control Flow
    elif opcode in (0x63, 0x6F, 0x67) or "pc_wdata" in diffs:
        subsystem = "branch"
        diagnosis = "Control-flow divergence in branch comparison or next-PC calculation logic."
        for idx, line in enumerate(lines):
            if "is_slti_blt_slt" in line or "alu_lts = $signed" in line:
                start_line = max(1, idx - 15)
                end_line = min(total_lines, idx + 45)
                break

    # Subsystem 3: Memory Interface / Bus Handshake
    elif any(f in diffs for f in ("mem_addr", "mem_wdata", "mem_wmask", "mem_rmask")) or opcode in (0x03, 0x23):
        subsystem = "memory"
        diagnosis = "Memory interface protocol or address offset divergence in lookahead bus controller."
        for idx, line in enumerate(lines):
            if "assign mem_la_addr" in line:
                start_line = max(1, idx - 10)
                end_line = min(total_lines, idx + 50)
                break

    # Subsystem 4: Register File / Writeback / Forwarding Hazard
    # Triggers when rd_wdata differs on an ALU/R/I-type instruction.
    # The classic writeback forwarding drop symptom: rd_addr is correct
    # (points to x6 for example) but rd_wdata carries a stale/unforwarded
    # value.  We also fall here when rd_addr itself disagrees.
    elif (("rd_wdata" in diffs or "rd_addr" in diffs)
          and opcode in (0x13, 0x33)
          and funct7 != 0x01):  # exclude RV32M (handled by multiplier subsystem)
        subsystem = "regfile"
        diagnosis = "Register file writeback or forwarding hazard drop detected. Check cpuregs_wrdata and latched_rd writeback path."
        for idx, line in enumerate(lines):
            if "cpuregs_wrdata =" in line or "cpuregs[latched_rd]" in line:
                start_line = max(1, idx - 10)
                end_line = min(total_lines, idx + 50)
                break

    # Subsystem 5: ALU Arithmetic & Bitwise Logic
    # Reached only when none of the above subsystems matched — typically a
    # pure ALU operator/muxer bug that isn't a forwarding hazard.
    elif opcode in (0x13, 0x33) and funct7 not in (0x01, 0x20) and diffs:
        subsystem = "alu"
        diagnosis = "ALU operator or bitwise multiplexer discrepancy. Check case-statement operator mux or funct3 decode."
        for idx, line in enumerate(lines):
            if "case (1'b1)" in line and idx > 1200:
                start_line = max(1, idx - 10)
                end_line = min(total_lines, idx + 55)
                break

    # Fallback: Default to ALU
    if subsystem == "unknown":
        subsystem = "alu"
        start_line = 1250
        end_line = 1300

    snippet_lines = lines[start_line - 1:end_line]
    numbered_snippet = "\n".join(f"{start_line + i:4d}: {line}" for i, line in enumerate(snippet_lines))

    return {
        "subsystem": subsystem,
        "start_line": start_line,
        "end_line": end_line,
        "total_snippet_lines": len(snippet_lines),
        "diagnosis": diagnosis,
        "snippet": "\n".join(snippet_lines),
        "numbered_snippet": numbered_snippet,
    }


def format_localized_repair_prompt(
    minimal_reproducer: dict,
    failure_record: dict,
    localization: dict,
    golden_register_state: dict | None = None,
) -> str:
    """Format structured prompt for Gemini with localized 30-80 line RTL slice."""
    insn_disasm = failure_record.get("insn_disasm", "")
    insn_hex = failure_record.get("insn_hex", "")
    diffs = failure_record.get("differences", {})

    diff_str = "\n".join(
        f"  - {field}: Expected (Golden ISS) = 0x{data.get('expected', 0):08x} ({data.get('expected', 0)}), "
        f"Actual (Hardware RTL) = 0x{data.get('actual', 0):08x} ({data.get('actual', 0)})"
        for field, data in diffs.items()
    )

    preload_str = ""
    if golden_register_state:
        nonzero = {k: v for k, v in golden_register_state.items() if v != 0}
        preload_str = f"\nPre-loaded Register State before failure:\n" + "\n".join(
            f"  {reg}: 0x{val:08x} ({val})" for reg, val in list(nonzero.items())[:8]
        )

    return f"""You are a RISC-V hardware verification and RTL repair engineer.
A hardware divergence has been detected between the PicoRV32 processor RTL and the pure Python golden reference.

=== MINIMAL FAILING REPRODUCER ===
Program ID: {minimal_reproducer.get('id')}
Failing Instruction: {insn_disasm} ({insn_hex})
Trace Length: {len(minimal_reproducer.get('words', []))} instructions

=== DISCREPANCY SIGNATURE ===
{diff_str}
{preload_str}

=== LOCALIZED HARDWARE SUBSYSTEM: {localization['subsystem'].upper()} ===
Diagnostic: {localization['diagnosis']}
Candidate Fault Block (Lines {localization['start_line']} to {localization['end_line']}):

```verilog
{localization['numbered_snippet']}
```

=== TASK ===
Analyze the discrepancy and the localized Verilog code block. Provide the corrected Verilog code block.
Your response must contain ONLY the complete repaired Verilog module or updated lines inside a ```verilog code block.
"""

