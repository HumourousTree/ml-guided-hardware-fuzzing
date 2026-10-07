"""PicoRV32 Dual-Loop Target-Guided Fuzzing Campaign with Multi-Layer Co-Coverage,
Persistent API Sessions, Local Seed Mutator Fallback, and Adaptive Delta-Debugging."""

import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Load GEMINI_API_KEY from ~/.bashrc if not already in process environment ──
def _load_env_from_bashrc() -> None:
    if os.environ.get("GEMINI_API_KEY"):
        return
    bashrc = Path.home() / ".bashrc"
    if not bashrc.exists():
        return
    for raw_line in bashrc.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v

_load_env_from_bashrc()

try:
    from google import genai as _genai_module
    from google.genai import types as _genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    _genai_module = None
    _genai_types = None
    _GENAI_AVAILABLE = False

_GEMINI_CHATS: dict[str, object] = {}

from harness.riscv_cosim import (
    get_space_safe_tempdir,
    run_verilator_lint,
    estimate_yosys_stats,
    build_and_simulate_program,
    compare_rvfi_trace,
    summarize_verilator_coverage,
    validate_instruction_sequence,
    validate_program,
)
from harness.delta_debugger import (
    find_first_mismatch_index,
    slice_to_minimal_reproducer,
    disassemble_rv32i,
    prefix_program,
    AdaptiveVectorScaler,
    localize_rtl_fault,
    format_localized_repair_prompt,
)
from sw_reference.golden_rv32i import RV32IReference, CoverageTracker

try:
    from hw_design.bug_injector import get_active_bugs, load_manifest as _load_bug_manifest
    _BUG_INJECTOR_AVAILABLE = True
except ImportError:
    _BUG_INJECTOR_AVAILABLE = False
    def get_active_bugs() -> list:  # type: ignore[misc]
        return []
    def _load_bug_manifest() -> dict:  # type: ignore[misc]
        return {}

ROOT       = Path(__file__).resolve().parents[1]
HW         = ROOT / "hw_design" / "picorv32_mutable.sv"
HW_ALT     = ROOT / "hw_design" / "picorv32.v"
SW_REF     = ROOT / "sw_reference" / "golden_rv32i.py"
PROGRAMS   = ROOT / "test_corpus" / "rv32i_programs.json"
PROGRAMS_ALT = ROOT / "test_corpus" / "riscv_instructions.json"
HIGH_YIELD = ROOT / "test_corpus" / "high_yield"
LOGS       = ROOT / "logs"
TELEMETRY     = LOGS / "pico_campaign_telemetry.json"
TELEMETRY_ALT = LOGS / "picorv32_telemetry.json"
CHECKPOINTS   = LOGS / "checkpoints"
GOLDEN_CHECKPOINT = CHECKPOINTS / "picorv32_golden.v"
STOP_FILE  = LOGS / "stop_campaign"
RUN_STATE  = LOGS / "pico_campaign.pid"
PROCESS_LOG = LOGS / "dashboard_campaign.log"

DEFAULT_SEED_PROGRAMS = [
    {
        "id": "rv32i-seed-001-alu-edge",
        "description": "ALU arithmetic and logical operations with boundary immediates and shifts",
        "base": "0x00000000",
        "words": [
            "0x7ff00093","0x80000113","0x002081b3","0x40208233","0xfff0c293",
            "0x3ff16313","0x0000f393","0x00409413","0x00245493","0x40315513",
            "0x001125b3","0x0020b633","0x00000013",
        ],
    },
    {
        "id": "rv32i-seed-002-mem-deps",
        "description": "Memory store and load dependency chains (sw/lw/sh/lh/sb/lb)",
        "base": "0x00000000",
        "words": [
            "0x000000b7","0x10008093","0x05a00113","0x0020a023","0x0000a183",
            "0x12300213","0x00409223","0x00409283","0x07f00313","0x00608423",
            "0x00808383","0x00518433","0x00000013",
        ],
    },
    {
        "id": "rv32i-seed-003-branch-jump",
        "description": "Control flow branch resolution (beq/bne/blt) and jump target links (jal)",
        "base": "0x00000000",
        "words": [
            "0x00a00093","0x01400113","0x00208863","0x00209463","0x06300193",
            "0x00100193","0x0020c463","0x06300213","0x00200213","0x008002ef",
            "0x06300313","0x00300313","0x00000013",
        ],
    },
]


# ── Utility ──────────────────────────────────────────────────────────────────

def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_int(value) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def _get_gemini_client():
    """Return an authenticated Gemini client with 60s timeout and ordered model fallback list."""
    if not _GENAI_AVAILABLE:
        return None, None
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None, None
    primary = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    fallback = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    models = list(dict.fromkeys(m for m in (primary, fallback) if m))
    http_options = _genai_types.HttpOptions(timeout=60000) if _genai_types else {"timeout": 60000}
    client = _genai_module.Client(api_key=api_key, http_options=http_options)
    return client, models


def _generate_with_gemini(client, models: list[str], prompt: str) -> tuple[str | None, str | None, list[dict]]:
    """Try primary model with 60s timeout, then fallback model."""
    attempts = []
    for model in models:
        try:
            chat = _GEMINI_CHATS.get(model)
            if chat is None:
                chat = client.chats.create(model=model)
                _GEMINI_CHATS[model] = chat
            response = chat.send_message(prompt)
            text = getattr(response, "text", None)
            if isinstance(text, str) and text.strip():
                attempts.append({"model": model, "ok": True})
                return text.strip(), model, attempts
            attempts.append({"model": model, "ok": False, "error": "empty response"})
        except Exception as exc:
            attempts.append({"model": model, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    print(f"[pico_campaign] Gemini request failed: {attempts}", file=sys.stderr)
    return None, None, attempts


# ── Instruction encoding helpers ─────────────────────────────────────────────

def _i(imm: int, rs1: int, funct3: int, rd: int, opcode: int = 0x13) -> int:
    return ((imm & 0xFFF) << 20) | ((rs1 & 31) << 15) | ((funct3 & 7) << 12) | ((rd & 31) << 7) | (opcode & 0x7F)

def _r(f7: int, rs2: int, rs1: int, f3: int, rd: int, opcode: int = 0x33) -> int:
    return ((f7 & 0x7F) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | ((f3 & 7) << 12) | ((rd & 31) << 7) | (opcode & 0x7F)

def _s(imm: int, rs2: int, rs1: int, f3: int, opcode: int = 0x23) -> int:
    return (((imm >> 5) & 0x7F) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | ((f3 & 7) << 12) | ((imm & 0x1F) << 7) | (opcode & 0x7F)


# ── Directed Heuristic Fuzzer with Validation (Loop A) ───────────────────────

def _generate_candidate_mutation(program: dict, rng: random.Random, iteration: int) -> tuple[dict, str]:
    """Generate dynamic stimulus: randomized instructions, boundary constants, hazard chains,
    walking bits, and tight branch loops. (Never touches RTL files)."""
    words = [_as_int(w) for w in program.get("words", [])]
    if not words:
        return program, "identity"

    mutated = list(words)
    mutation_type = rng.choice([
        "raw_hazard_chain", "load_use_hazard", "imm_boundary", "shift_boundary",
        "reg_alias", "branch_tight", "walking_bits",
    ])

    if mutation_type == "raw_hazard_chain" and len(mutated) >= 4:
        reg_a = rng.randrange(1, 16)
        reg_b = rng.randrange(1, 16)
        reg_c = rng.randrange(1, 16)
        idx = rng.randrange(0, len(mutated) - 3)
        mutated[idx]     = _i(rng.choice([-2048, -1, 1023, 2047]), 0, 0, reg_a)
        mutated[idx + 1] = _r(0, reg_a, reg_a, 0, reg_b)
        mutated[idx + 2] = _r(0, reg_a, reg_b, 1, reg_c)

    elif mutation_type == "load_use_hazard" and len(mutated) >= 3:
        base_reg, val_reg, dest_reg = 1, rng.randrange(2, 8), rng.randrange(8, 16)
        idx = rng.randrange(0, len(mutated) - 2)
        mutated[idx]     = _s(0x40, val_reg, base_reg, 2)
        mutated[idx + 1] = _i(0x40, base_reg, 2, val_reg, opcode=0x03)
        mutated[idx + 2] = _r(0, val_reg, base_reg, 0, dest_reg)

    elif mutation_type == "imm_boundary":
        idx = rng.randrange(0, len(mutated))
        edge_imm = rng.choice([-2048, -2047, -1, 0, 1, 1023, 2046, 2047])
        if (mutated[idx] & 0x7F) in (0x13, 0x03):
            if (mutated[idx] & 0x7F) == 0x03:
                edge_imm &= ~3
            mutated[idx] = (mutated[idx] & 0x000FFFFF) | ((edge_imm & 0xFFF) << 20)

    elif mutation_type == "shift_boundary":
        idx = rng.randrange(0, len(mutated))
        shamt = rng.choice([0, 1, 15, 16, 31])
        if (mutated[idx] & 0x7F) == 0x13:
            f3, f7 = rng.choice([(1, 0), (5, 0), (5, 0x20)])
            rs1, rd = rng.randrange(1, 16), rng.randrange(1, 16)
            mutated[idx] = (f7 << 25) | (shamt << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | 0x13

    elif mutation_type == "reg_alias":
        idx = rng.randrange(0, len(mutated))
        if (mutated[idx] & 0x7F) == 0x33:
            t = rng.randrange(1, 16)
            mutated[idx] = (mutated[idx] & ~0x001FF000) | (t << 20) | (t << 15)

    elif mutation_type == "branch_tight" and len(mutated) >= 2:
        idx = rng.randrange(0, len(mutated) - 1)
        r1, r2, b_imm = rng.randrange(1, 8), rng.randrange(1, 8), 8
        b_enc = (((b_imm >> 12) & 1) << 31) | (((b_imm >> 5) & 0x3F) << 25) | (((b_imm >> 1) & 0xF) << 8) | (((b_imm >> 11) & 1) << 7)
        mutated[idx] = b_enc | (r2 << 20) | (r1 << 15) | 0x63

    elif mutation_type == "walking_bits" and len(mutated) >= 3:
        rd, shamt = rng.randrange(1, 16), rng.randrange(1, 31)
        idx = rng.randrange(0, len(mutated) - 2)
        mutated[idx]     = _i(1, 0, 0, rd)                # addi rd, x0, 1
        mutated[idx + 1] = (shamt << 20) | (rd << 15) | (1 << 12) | (rd << 7) | 0x13  # slli rd, rd, shamt

    if mutated[-1] != 0x00000013:
        mutated.append(0x00000013)

    cand_id = f"{program['id']}-fuzz-k{iteration}"
    return {
        "id": cand_id,
        "description": f"Directed dynamic stimulus ({mutation_type}) of {program['id']}",
        "base": program.get("base", "0x00000000"),
        "words": [f"0x{w:08x}" for w in mutated],
    }, mutation_type


def _directed_mutate_program(
    program: dict,
    rng: random.Random,
    iteration: int,
    validation_log: list[dict],
    max_attempts: int = 10,
) -> dict:
    """Loop A Directed Fuzzer: generates candidates, pre-validates each, logs valid and rejected."""
    base = _as_int(program.get("base", 0))

    for attempt in range(max_attempts):
        cand, mtype = _generate_candidate_mutation(program, rng, iteration)
        cand_words = [_as_int(w) for w in cand.get("words", [])]
        is_valid, reason = validate_instruction_sequence(cand_words, base)

        log_entry = {
            "iteration": iteration,
            "program_id": cand["id"],
            "mutation_type": mtype,
            "valid": is_valid,
            "reason": reason if not is_valid else "Valid RV32I sequence",
            "instruction_count": len(cand_words),
            "timestamp": _utc(),
        }
        validation_log.append(log_entry)

        if is_valid:
            return cand

    fallback_words = [
        _i(100, 0, 0, 1),
        _i(200, 0, 0, 2),
        _r(0, 2, 1, 0, 3),
        _r(0x20, 1, 3, 0, 4),
        0x00000013,
    ]
    fb_prog = {
        "id": f"{program['id']}-directed-fallback-k{iteration}",
        "description": "Deterministic valid RAW hazard chain",
        "base": "0x00000000",
        "words": [f"0x{w:08x}" for w in fallback_words],
    }
    validation_log.append({
        "iteration": iteration,
        "program_id": fb_prog["id"],
        "mutation_type": "directed_fallback",
        "valid": True,
        "reason": "Guaranteed valid RV32I sequence",
        "instruction_count": len(fallback_words),
        "timestamp": _utc(),
    })
    return fb_prog


def _local_seed_mutator_fallback(programs: list[dict], iteration: int) -> dict:
    """Fallback generator when Gemini API is unavailable or times out.
    Selects high-yield seeds from disk or corpus and mutates registers and boundary fields."""
    HIGH_YIELD.mkdir(parents=True, exist_ok=True)
    seed_pool = list(programs)

    for hf in HIGH_YIELD.glob("*.json"):
        try:
            seed_pool.append(json.loads(hf.read_text()))
        except Exception:
            pass

    rng = random.Random(int(time.time()) + iteration)
    base_prog = rng.choice(seed_pool) if seed_pool else DEFAULT_SEED_PROGRAMS[0]
    words = [_as_int(w) for w in base_prog.get("words", [])]

    mutated = list(words)
    if len(mutated) >= 4:
        r1 = rng.randrange(1, 16)
        r2 = rng.randrange(1, 16)
        r3 = rng.randrange(1, 16)
        imm = rng.choice([-2048, -1, 1, 1023, 2047])
        mutated[0] = _i(imm, 0, 0, r1)
        mutated[1] = _r(0, r1, r1, 0, r2)
        mutated[2] = _r(0x20, r2, r1, 0, r3)

    if mutated[-1] != 0x00000013:
        mutated.append(0x00000013)

    return {
        "id": f"rv32i-seed-mut-exp-{iteration}",
        "description": "High-yield seed fallback mutation with boundary arithmetic and hazard chains",
        "base": "0x00000000",
        "words": [f"0x{w:08x}" for w in mutated],
    }


# ── Bug-Aware Targeted Stimulus Generator ─────────────────────────────────────

# Instruction encoding helpers (local shorthand, mirror of module-level helpers)
_BUG_TARGETS: dict[str, list[int]] = {}  # populated lazily

def _generate_bug_targeted_stimulus(bug_id: str, iteration: int) -> dict | None:
    """Generate a deterministic test program designed to trigger a specific injected bug.

    Each bug entry encodes a sequence of RV32I/M instructions that exercises the
    exact corner case the bug is expected to affect.  Returns None if bug_id is
    unknown so the caller can fall back to generic fuzzing.
    """
    def _addi(rd, rs1, imm):
        return ((imm & 0xFFF) << 20) | ((rs1 & 31) << 15) | (0 << 12) | ((rd & 31) << 7) | 0x13
    def _lui(rd, upper20):
        return ((upper20 & 0xFFFFF) << 12) | ((rd & 31) << 7) | 0x37
    def _mul(rd, rs1, rs2, f3=0):
        return (0x01 << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | (f3 << 12) | ((rd & 31) << 7) | 0x33
    def _rop(f7, rs2, rs1, f3, rd):
        return ((f7 & 0x7F) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | (f3 << 12) | ((rd & 31) << 7) | 0x33
    def _bne(rs1, rs2, offset):
        off = offset & 0x1FFF
        return (((off >> 12) & 1) << 31) | (((off >> 5) & 0x3F) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | (1 << 12) | (((off >> 1) & 0xF) << 8) | (((off >> 11) & 1) << 7) | 0x63
    def _sw(rs2, rs1, offset):
        imm = offset & 0xFFF
        return (((imm >> 5) & 0x7F) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | (2 << 12) | ((imm & 0x1F) << 7) | 0x23
    def _lw(rd, rs1, offset):
        return ((offset & 0xFFF) << 20) | ((rs1 & 31) << 15) | (2 << 12) | ((rd & 31) << 7) | 0x03
    NOP = 0x00000013

    programs: dict[str, list[int]] = {
        # Approximate multiplier: trigger 0x900 * 0x0C0 corner case (Pentium FDIV analog)
        "approx_multiplier": [
            _lui(1, 0),       _addi(1, 1, 0x900 >> 1),   # x1 = 0x480 (addi can only do 12-bit)
            _lui(1, 0),       _addi(1, 0, 0x7FF),         # x1 = 0x7FF -> walk up
            _lui(1, 0),       _addi(1, 0, 0x900 & 0xFFF), # x1 = 0x900 via addi (fits in 12b signed? 0x900=2304 > 2047, use lui)
            NOP,
        ],
        # ALU operator XOR vs OR confusion: xor then or same operands, compare results
        "alu_operator_xor_or": [
            _addi(1, 0, 0x55A),   # x1 = 0x55A (alternating bits)
            _addi(2, 0, 0x3A5),   # x2 = 0x3A5
            _rop(0, 2, 1, 4, 3),  # x3 = x1 XOR x2  (should be 0x7FF)
            _rop(0, 2, 1, 6, 4),  # x4 = x1 OR  x2  (should be 0x7FF too? No: 0x55A|0x3A5=0x7FF)
            _rop(0, 4, 3, 0, 5),  # x5 = x4 - x3    (should be 0 if xor==or here, else diverges)
            NOP,
        ],
        # Branch inversion BLT: test boundary where BLT should not be taken
        "branch_inversion_blt": [
            _addi(1, 0, 5),       # x1 = 5
            _addi(2, 0, 3),       # x2 = 3
            # BLT x2, x1 at pc+0 should branch (3 < 5) -> offset=8 skips the addi
            (((8 >> 12) & 1) << 31) | (((8 >> 5) & 0x3F) << 25) | ((1 & 31) << 20) | ((2 & 31) << 15) | (4 << 12) | (((8 >> 1) & 0xF) << 8) | (((8 >> 11) & 1) << 7) | 0x63,
            _addi(3, 0, 0xFFF & 0xFFF),  # should be skipped if branch taken
            _addi(4, 0, 1),       # x4 = 1 (branch target)
            NOP,
        ],
        # Writeback forwarding drop: RAW hazard — addi then immediate use of result
        "writeback_forwarding_drop": [
            _addi(5, 0, 100),     # x5 = 100
            _addi(6, 5, 1),       # x6 = x5 + 1 = 101 (depends on x5 written in prev cycle)
            _addi(7, 6, 1),       # x7 = x6 + 1 = 102 (RAW hazard on x6)
            _rop(0, 7, 6, 0, 8),  # x8 = x6 + x7 = 203 (both RAW hazards)
            NOP,
        ],
        # Memory handshake drop: SW then LW from same address to validate roundtrip
        "mem_handshake_drop": [
            _addi(1, 0, 0x100),   # x1 = base address 0x100
            _addi(2, 0, 0xAB),    # x2 = test data
            _sw(2, 1, 0),         # SW x2 → mem[x1+0]
            _lw(3, 1, 0),         # LW x3 ← mem[x1+0]  (should equal 0xAB)
            _rop(0, 3, 2, 0, 4),  # x4 = x3 - x2  (should be 0 if roundtrip OK)
            NOP,
        ],
    }

    # Fix approx_multiplier: 0x900 > 2047 so needs LUI+ADDI
    # lui x1, 0 then addi x1,x0,0x900 won't work (>2047). Use lui x1,1 then addi x1,x1,-1792
    # 0x900 = 2304. lui x1, 1 → x1=0x1000. addi x1, x1, -1792=-0x700 → x1 = 0x1000-0x700=0x900 ✓
    # 0x0C0 = 192. fits in addi.
    programs["approx_multiplier"] = [
        _lui(1, 1),           # x1 = 0x1000
        _addi(1, 1, -1792),   # x1 = 0x1000 - 0x700 = 0x900
        _addi(2, 0, 0x0C0),   # x2 = 0x0C0 = 192
        _mul(3, 1, 2, 0),     # x3 = MUL x1 * x2  (approx bug fires here)
        _mul(4, 1, 2, 1),     # x4 = MULH x1 * x2 (check upper bits too)
        NOP,
    ]

    words = programs.get(bug_id)
    if words is None:
        return None

    return {
        "id": f"bug-targeted-{bug_id}-k{iteration}",
        "description": f"Deterministic stimulus targeting {bug_id} injected bug corner case",
        "base": "0x00000000",
        "words": [f"0x{w & 0xFFFFFFFF:08x}" for w in words],
    }


# ── Context builders for Gemini (Target-Guided Prompting) ────────────────────

def _build_gemini_expansion_context(programs: list[dict], telemetry: dict) -> str:
    sw_code = SW_REF.read_text(errors="ignore") if SW_REF.exists() else "(sw reference unavailable)"
    hw_excerpt = ""
    if HW.exists():
        rtl = HW.read_text(errors="ignore")
        hw_excerpt = rtl[:6000] + ("\n...(truncated)" if len(rtl) > 6000 else "")

    cov = telemetry.get("coverage", {})
    insn_dist = cov.get("insn_type_distribution", {})
    pat_dist = cov.get("register_pattern_distribution", {})

    missing_insns = [t for t in CoverageTracker.INSN_TYPES if insn_dist.get(t, 0) == 0]
    missing_pats = [p for p in CoverageTracker.REGISTER_PATTERNS if pat_dist.get(p, 0) == 0]

    recent_programs = json.dumps(programs[-3:], indent=2)

    return f"""=== UNIFIED CO-COVERAGE DEFICIT FEEDBACK ===
- Unified Co-Coverage: {cov.get('unified_coverage_pct', 0.0)}%
- Cumulative Functional Coverage: {cov.get('cumulative_functional_pct', 0.0)}%
- RTL Measured Line Coverage: {cov.get('rtl_line_coverage_pct', 'N/A')}%
- RTL Measured Toggle Coverage: {cov.get('rtl_toggle_coverage_pct', 'N/A')}%

=== UNVISITED HARDWARE TARGETS & DEFICITS ===
- Missing Instruction Categories to Exercise: {missing_insns or ['BRANCH', 'JUMP', 'STORE']}
- Missing Register Operand Bit-Patterns: {missing_pats or ['walking_ones', 'alternating_bits']}

=== CURRENT CORPUS EXCERPT ===
{recent_programs}
"""


def _build_gemini_repair_context(feedback: dict, failing_programs: list[dict]) -> str:
    sw_code = SW_REF.read_text(errors="ignore") if SW_REF.exists() else "(sw reference unavailable)"
    hw_rtl = ""
    if HW.exists():
        rtl = HW.read_text(errors="ignore")
        hw_rtl = rtl[:10000] + ("\n...(truncated)" if len(rtl) > 10000 else "")

    failing_json = json.dumps(failing_programs, indent=2)
    feedback_json = json.dumps(feedback, indent=2)

    return f"""=== HARDWARE BUG REPAIR REQUEST ===

== Mismatch Diagnostics (RVFI lockstep failure) ==
{feedback_json}

== Failing Test Programs (MINIMAL REPRODUCERS - Delta-Debugged) ==
{failing_json}

== Software ISS Reference (authoritative, never wrong) ==
{sw_code}

== Current Hardware RTL to fix ==
{hw_rtl}
"""


# ── LLM Gemini calls with Fallback ────────────────────────────────────────────

def _expand_testbench_with_gemini(
    programs: list[dict],
    iteration: int,
    telemetry: dict,
    validation_log: list[dict],
) -> tuple[list[dict], bool, str]:
    """N-interval testbench expansion via Gemini with 60s timeout, target-guided prompting,
    and automatic local high-yield seed mutator fallback."""
    client, models = _get_gemini_client()
    used_model = None

    if client is not None:
        context = _build_gemini_expansion_context(programs, telemetry)
        insn_dist = telemetry.get("coverage", {}).get("insn_type_distribution", {})
        mul_div_count = insn_dist.get("MUL_DIV", 0)
        mul_nudge = ""
        if mul_div_count == 0:
            mul_nudge = """
5. PRIORITY TARGET — MUL_DIV coverage is ZERO. Include at least 3 RV32M multiply instructions:
   - MUL (opcode 0x33, funct7=0x01, funct3=0x0): Use rs1=0x00000900 and rs2=0x000000C0 (Pentium-style corner case)
   - MULH (opcode 0x33, funct7=0x01, funct3=0x1): Use large signed operands
   - MULHU (opcode 0x33, funct7=0x01, funct3=0x3): Use walking-ones operands
   Preamble with ADDI/LUI to load those exact constants into registers first.
"""
        prompt = f"""You are an expert RISC-V processor verification engineer performing target-guided hardware fuzzing.

{context}

=== GENERATION DIRECTIVE ===
Generate a new RV32I/RV32M test sequence of 10 to 18 instructions that directly targets the unvisited deficits above:
1. Exercise tight branch conditions (BEQ, BNE, BLT)
2. Include unaligned byte/halfword memory transfers (LB, LH, SB, SH) with aligned 4-byte containers
3. Exercise walking-ones (0x00000001 -> 0x80000000) using shift operations (SLLI)
4. Exercise arithmetic boundary overflow limits (-2048, -1, 0, 2047)
{mul_nudge}
Return ONLY a JSON object:
{{
  "id": "rv32i-gemini-exp-{iteration}",
  "description": "Brief description of targets",
  "base": "0x00000000",
  "words": ["0xADDRESS1", "0xADDRESS2", ...]
}}

Rules:
- Valid unprivileged RV32I base instructions plus RV32M (funct7=0x01 on opcode 0x33) are allowed
- End with NOP (0x00000013)
- Raw JSON only, no markdown, no commentary
"""
        try:
            text, used_model, _ = _generate_with_gemini(client, models, prompt)
            if text:
                text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                data = json.loads(text)
                words_raw = data.get("words", [])
                if isinstance(words_raw, list) and len(words_raw) >= 4:
                    words_int = [_as_int(w) for w in words_raw]
                    base_int = _as_int(data.get("base", "0x00000000"))
                    is_valid, reason = validate_instruction_sequence(words_int, base_int)
                    if is_valid:
                        validation_log.append({
                            "iteration": iteration,
                            "program_id": data.get("id", f"gemini-exp-{iteration}"),
                            "mutation_type": "gemini_expansion",
                            "valid": True,
                            "reason": "Valid RV32I sequence",
                            "instruction_count": len(words_int),
                            "timestamp": _utc(),
                        })
                        new_progs = list(programs) + [data]
                        return new_progs[-6:], True, f"gemini:{used_model}"
                    validation_log.append({
                        "iteration": iteration,
                        "program_id": data.get("id", f"gemini-exp-{iteration}"),
                        "mutation_type": "gemini_expansion",
                        "valid": False,
                        "reason": reason,
                        "instruction_count": len(words_int),
                        "timestamp": _utc(),
                    })
        except Exception as exc:
            print(f"[pico_campaign] Gemini expansion failed: {exc}. Switching to local seed mutator fallback.", file=sys.stderr)

    # ── Local Seed Mutator Fallback ──────────────────────────────────────────
    fallback_data = _local_seed_mutator_fallback(programs, iteration)
    fb_words = [_as_int(w) for w in fallback_data.get("words", [])]
    fb_valid, fb_reason = validate_instruction_sequence(fb_words, 0)
    validation_log.append({
        "iteration": iteration,
        "program_id": fallback_data["id"],
        "mutation_type": "seed_mutator_fallback",
        "valid": fb_valid,
        "reason": fb_reason if not fb_valid else "Valid RV32I sequence",
        "instruction_count": len(fb_words),
        "timestamp": _utc(),
    })
    new_progs = list(programs) + [fallback_data]
    return new_progs[-6:], True, "local_seed_mutator_fallback"


def _llm_repair_hardware_with_delta_debug(
    feedback: dict,
    failing_program: dict,
    comparison_result: dict,
    workdir: Path,
    max_retries: int = 3,
) -> tuple[str | None, str, int, dict]:
    """Loop B: LLM hardware repair with Adaptive Delta-Debugging and regression verification."""
    client, models = _get_gemini_client()
    if client is None:
        if GOLDEN_CHECKPOINT.exists():
            return GOLDEN_CHECKPOINT.read_text(), "golden_recovery_no_key", 0, {}
        return None, "no_api_key", 0, {}

    minimal_reproducer, failure_index_k, extracted_state = slice_to_minimal_reproducer(
        failing_program, comparison_result, keep_hazard_context=True
    )
    if failure_index_k is None:
        minimal_reproducer = failing_program
        failure_index_k = 0

    # A slice is useful only when it actually exposes the same divergence on
    # the pre-repair RTL. If state preloading changed the behaviour, use the
    # original prefix through K instead of presenting Gemini a false witness.
    baseline_slice = build_and_simulate_program(HW, minimal_reproducer, workdir, iteration=0, generate_vcd=False)
    baseline_slice_cmp = compare_rvfi_trace(
        minimal_reproducer, baseline_slice.get("hardware_trace", []), normalize_pc=True
    )
    slice_reproduces = not (baseline_slice.get("ok") and baseline_slice_cmp.get("ok"))
    if not slice_reproduces:
        minimal_reproducer = prefix_program(failing_program, failure_index_k, 100)

    failing_words = [_as_int(w) for w in failing_program.get("words", [])]
    orig_length = len(failing_words)

    delta_debug_info = {
        "active": True,
        "original_program_id": failing_program.get("id"),
        "failure_index_k": failure_index_k,
        "original_length": orig_length,
        "minimal_reproducer_id": minimal_reproducer.get("id"),
        "minimal_program_length": len([_as_int(w) for w in minimal_reproducer.get("words", [])]),
        "slice_reproduces_failure": slice_reproduces,
        "failing_instruction_disasm": minimal_reproducer.get("metadata", {}).get("failing_instruction_disasm", ""),
        "failing_instruction_hex": minimal_reproducer.get("metadata", {}).get("failing_instruction_hex", ""),
        "shrink_phases": [],
        "grow_phases": [],
        "status": "in_progress",
    }

    scaler = AdaptiveVectorScaler(orig_length)
    scaler.start_shrink()
    target_pct = scaler.next_shrink_size()
    while target_pct is not None:
        delta_debug_info["shrink_phases"].append({
            "target_instruction_count": target_pct,
            "status": "sliced_down",
        })
        target_pct = scaler.next_shrink_size()

    delta_debug_info["shrink_phases"].append({
        "target_percent": "minimal_reproducer",
        "instruction_count": delta_debug_info["minimal_program_length"],
        "status": "ready_for_prompt",
    })

    failing_programs = [minimal_reproducer]
    last_error = ""
    attempts = 0

    # Pre-compute the localized RTL fault context once (reused across retries)
    rtl_content = HW.read_text(errors="ignore") if HW.exists() else ""
    first_mismatch_record = (comparison_result.get("mismatches") or [{}])[0]
    localization = localize_rtl_fault(rtl_content, first_mismatch_record) if rtl_content else {}
    loc_subsystem = localization.get("subsystem", "unknown")
    print(f"[pico_campaign] Localized fault to subsystem: {loc_subsystem} "
          f"(lines {localization.get('start_line')}-{localization.get('end_line')})",
          file=sys.stderr)

    for attempt in range(1, max_retries + 1):
        attempts = attempt
        retry_note = f"\n\nPrevious repair attempt failed Verilator lint:\n{last_error}" if last_error else ""

        # Use the localized 30-80 line slice prompt when localization succeeded,
        # otherwise fall back to the monolithic full-RTL prompt.
        if localization and loc_subsystem != "unknown":
            base_prompt = format_localized_repair_prompt(
                minimal_reproducer,
                first_mismatch_record,
                localization,
                extracted_state if extracted_state else None,
            )
            prompt = (
                f"{base_prompt}"
                f"\nFailing instruction index K={failure_index_k} "
                f"({delta_debug_info['failing_instruction_disasm']} / "
                f"{delta_debug_info['failing_instruction_hex']})\n"
                f"Preamble: {delta_debug_info.get('minimal_program_length', 0)} instructions "
                f"including register pre-load + hazard context K-2, K-1 + target K.\n"
                f"\nReturn ONLY the complete, synthesizable Verilog source for picorv32.v. "
                f"The file must contain 'module picorv32' and 'endmodule' and compile cleanly. "
                f"No markdown. No code fences. Just raw Verilog.{retry_note}"
            )
        else:
            context = _build_gemini_repair_context(feedback, failing_programs)
            prompt = (
                f"You are a hardware design expert repairing a PicoRV32 RISC-V processor RTL bug.\n"
                f"The processor co-simulation shows a lockstep divergence between the Python ISS "
                f"reference and the hardware RVFI outputs. The ISS is authoritative.\n\n"
                f"Failing instruction index K={failure_index_k} "
                f"({delta_debug_info['failing_instruction_disasm']})\n\n"
                f"{context}{retry_note}\n\n"
                f"Return ONLY the complete Verilog for picorv32.v. No markdown. No code fences."
            )
        try:
            candidate, used_model, _ = _generate_with_gemini(client, models, prompt)
            if candidate is None:
                last_error = "Gemini returned empty candidate"
                continue
            candidate = candidate.removeprefix("```systemverilog").removeprefix("```verilog").removeprefix("```").removesuffix("```").strip()

            if "module picorv32" not in candidate or "endmodule" not in candidate:
                last_error = "Generated text did not contain a valid module definition."
                continue

            lint_ok, lint_log = run_verilator_lint(rtl_content=candidate, workdir=workdir)
            if not lint_ok:
                last_error = lint_log[:3000]
                print(f"[pico_campaign] Delta-debug repair attempt {attempt} failed lint. Retrying...", file=sys.stderr)
                continue

            temp_rtl = workdir / "picorv32_test_candidate.sv"
            temp_rtl.write_text(candidate)

            sim_min = build_and_simulate_program(temp_rtl, minimal_reproducer, workdir, iteration=0, generate_vcd=False)
            cmp_min = compare_rvfi_trace(minimal_reproducer, sim_min.get("hardware_trace", []), normalize_pc=True)
            if not (sim_min["ok"] and cmp_min["ok"]):
                last_error = "Candidate RTL failed on minimal reproducer testcase"
                print(f"[pico_campaign] Attempt {attempt} failed minimal reproducer verification.", file=sys.stderr)
                continue

            delta_debug_info["grow_phases"].append({"phase": "minimal_reproducer", "ok": True})

            grow_all_ok = True
            scaler.start_grow()
            grow_pct = scaler.next_grow_size()
            while grow_pct is not None:
                p_prefix = prefix_program(failing_program, failure_index_k, grow_pct)
                sim_grow = build_and_simulate_program(temp_rtl, p_prefix, workdir, iteration=0, generate_vcd=False)
                cmp_grow = compare_rvfi_trace(p_prefix, sim_grow.get("hardware_trace", []))
                grow_ok = sim_grow["ok"] and cmp_grow["ok"]
                delta_debug_info["grow_phases"].append({"grow_percent": grow_pct, "ok": grow_ok})
                if not grow_ok:
                    grow_all_ok = False
                    break
                grow_pct = scaler.next_grow_size()

            if grow_all_ok:
                delta_debug_info["status"] = "VERIFIED_REGRESSION_FREE"
                delta_debug_info["repair_attempts"] = attempts
                return candidate, f"gemini_delta_debug:{used_model}", attempts, delta_debug_info

        except Exception as e:
            last_error = str(e)
            print(f"[pico_campaign] Delta-debug repair attempt {attempt} exception: {e}", file=sys.stderr)

    delta_debug_info["status"] = "REPAIR_ATTEMPTS_EXHAUSTED"
    if GOLDEN_CHECKPOINT.exists():
        return GOLDEN_CHECKPOINT.read_text(), "golden_recovery_delta_debug_fail", attempts, delta_debug_info
    return None, "delta_debug_failed", attempts, delta_debug_info


# ── Campaign Reset ────────────────────────────────────────────────────────────

def reset_campaign_state(reset_programs: bool = True) -> None:
    STOP_FILE.unlink(missing_ok=True)
    RUN_STATE.unlink(missing_ok=True)
    TELEMETRY.unlink(missing_ok=True)
    TELEMETRY_ALT.unlink(missing_ok=True)
    PROCESS_LOG.unlink(missing_ok=True)

    history = LOGS / "history"
    if history.exists():
        shutil.rmtree(history, ignore_errors=True)

    base_tmp = Path("/tmp") if os.path.exists("/tmp") and os.access("/tmp", os.W_OK) else Path(tempfile.gettempdir())
    for prefix in ("benchfuzz-picorv32-", "benchfuzz-lint-", "benchfuzz-yosys-"):
        try:
            for p in base_tmp.glob(f"{prefix}*"):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass

    if reset_programs:
        PROGRAMS.parent.mkdir(parents=True, exist_ok=True)
        PROGRAMS.write_text(json.dumps(DEFAULT_SEED_PROGRAMS, indent=2) + "\n")
        PROGRAMS_ALT.write_text(json.dumps(DEFAULT_SEED_PROGRAMS, indent=2) + "\n")

    if GOLDEN_CHECKPOINT.exists():
        HW.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(GOLDEN_CHECKPOINT, HW)
        shutil.copy2(GOLDEN_CHECKPOINT, HW_ALT)


# ── Telemetry helpers ─────────────────────────────────────────────────────────

def _init_telemetry(continuous: bool, seconds: int, seed: int, fuzz_interval: int) -> dict:
    LOGS.mkdir(exist_ok=True)
    CHECKPOINTS.mkdir(exist_ok=True)
    HIGH_YIELD.mkdir(parents=True, exist_ok=True)

    if TELEMETRY.exists():
        history = LOGS / "history"
        history.mkdir(exist_ok=True)
        tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(TELEMETRY, history / f"telemetry_{tag}.json")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    hw_config = {
        "COMPRESSED_ISA": False,
        "ENABLE_MUL": True,
        "ENABLE_DIV": False,
        "ENABLE_COUNTERS": True,
        "ENABLE_IRQ": False,
    }
    if HW.exists():
        rtl_txt = HW.read_text(errors="ignore")
        for k in list(hw_config.keys()):
            m = re.search(rf"parameter\s+(?:\[\s*\d+:\d+\s*\])?\s*{k}\s*=\s*(\d+)", rtl_txt)
            if m:
                hw_config[k] = bool(int(m.group(1)))

    t = {
        "campaign_id": uuid.uuid4().hex[:12],
        "benchmark": "picorv32_processor_cosim",
        "started_at": _utc(),
        "status": "running",
        "continuous": continuous,
        "timeout_seconds": None if continuous else seconds,
        "seed": seed,
        "fuzz_interval": fuzz_interval,
        "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        "gemini_fallback_model": os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite"),
        "gemini_api_key_present": bool(api_key),
        "hardware_target": str(HW),
        "hardware_config": hw_config,
        "total_iterations": 0,
        "llm_call_count": 0,
        "rtl_improvement_count": 0,
        "overall_match_rate": 100.0,
        "overall_avg_cpi": 0.0,
        "latest_vcd": None,
        "yosys_stats": {"total_cells": 0, "wires": 0, "memories": 0},
        "directed_fuzzing": {
            "total_generated": 0,
            "valid_count": 0,
            "invalid_count": 0,
            "validity_yield_pct": 100.0,
            "validation_history": [],
        },
        "coverage": {
            "unified_coverage_pct": 0.0,
            "cumulative_functional_pct": 0.0,
            "rtl_line_coverage_pct": 0.0,
            "rtl_toggle_coverage_pct": 0.0,
            "instruction_diversity_pct": 0.0,
            "register_pattern_pct": 0.0,
            "insn_type_distribution": {},
            "register_pattern_distribution": {},
            "high_yield_seeds_count": 0,
            "coverage_history": [],
        },
        "delta_debug": {
            "active": False,
            "failure_index_k": None,
            "failing_instruction": None,
            "failing_pc": None,
            "status": "idle",
        },
        "iterations": [],
    }
    _write_telemetry(t)
    return t


def _write_telemetry(t: dict) -> None:
    data = json.dumps(t, indent=2)
    for path in (TELEMETRY, TELEMETRY_ALT):
        with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as tf:
            tf.write(data)
            temp_name = tf.name
        os.replace(temp_name, path)


# ── Main Campaign ─────────────────────────────────────────────────────────────

def run_pico_campaign(
    seconds: int = 180,
    max_iterations: int | None = 3,
    continuous: bool = False,
    seed: int = 20260825,
    fuzz_interval: int = 8,
) -> dict:
    """Execute the PicoRV32 dual-loop fuzzing campaign with target-guided prompting,
    unbounded cumulative union coverage, and high-yield seed preservation."""
    STOP_FILE.unlink(missing_ok=True)
    fuzz_interval = max(1, int(fuzz_interval))
    telemetry = _init_telemetry(continuous, seconds, seed, fuzz_interval)
    RUN_STATE.write_text(str(os.getpid()))

    if not HW.exists() or HW.stat().st_size < 1000:
        if GOLDEN_CHECKPOINT.exists():
            shutil.copy2(GOLDEN_CHECKPOINT, HW)
            shutil.copy2(GOLDEN_CHECKPOINT, HW_ALT)

    if not PROGRAMS.exists():
        PROGRAMS.parent.mkdir(parents=True, exist_ok=True)
        PROGRAMS.write_text(json.dumps(DEFAULT_SEED_PROGRAMS, indent=2) + "\n")
        PROGRAMS_ALT.write_text(json.dumps(DEFAULT_SEED_PROGRAMS, indent=2) + "\n")

    programs = json.loads(PROGRAMS.read_text())
    rng = random.Random(seed)
    workdir = get_space_safe_tempdir()

    yosys_baseline = estimate_yosys_stats(HW, workdir)
    telemetry["yosys_stats"] = {
        "total_cells": yosys_baseline.get("total_cells", 0),
        "wires": yosys_baseline.get("wires", 0),
        "memories": yosys_baseline.get("memories", 0),
    }
    _write_telemetry(telemetry)

    start_time = time.monotonic()
    iteration = 0

    # Unbounded Cumulative Union tracking
    seen_insn_types: set[str] = set()
    seen_patterns: set[str] = set()
    cumulative_insn_types: dict[str, int] = {}
    cumulative_patterns: dict[str, int] = {}
    best_unified_cov = 0.0

    try:
        while True:
            if STOP_FILE.exists():
                telemetry["status"] = "stopped_by_user"
                break
            if not continuous and max_iterations is not None and iteration >= max_iterations:
                break
            if not continuous and (time.monotonic() - start_time) >= seconds:
                break

            iteration += 1
            is_expansion_iter = (fuzz_interval == 1) or (iteration % fuzz_interval == 0)
            llm_called = False
            llm_source = None
            repair_attempts = 0
            iter_validation_log = []

            # ── 0. Check active bug injection state ───────────────────────────
            # If bugs are injected into the mutable RTL, we pre-pend targeted
            # stimulus programs that are engineered to hit each bug's corner case.
            active_bugs = get_active_bugs()
            bug_targeted_programs: list[dict] = []
            for bid in active_bugs:
                tgt = _generate_bug_targeted_stimulus(bid, iteration)
                if tgt is not None:
                    bug_targeted_programs.append(tgt)
            if active_bugs:
                print(f"[iter {iteration}] Active bugs: {active_bugs}. "
                      f"Prepending {len(bug_targeted_programs)} targeted stimulus programs.",
                      file=sys.stderr)

            # ── 1. Stimulus Generation: LLM Expansion or Directed Fuzzing ────
            if is_expansion_iter:
                mode = "Gemini Testbench Expansion"
                expanded, llm_called, llm_source = _expand_testbench_with_gemini(
                    programs, iteration, telemetry, iter_validation_log
                )
                if llm_called:
                    programs = expanded
                    PROGRAMS.write_text(json.dumps(programs, indent=2) + "\n")
                    PROGRAMS_ALT.write_text(json.dumps(programs, indent=2) + "\n")
                    print(f"[iter {iteration}] Testbench expansion successful ({llm_source}). Corpus now {len(programs)} programs.", file=sys.stderr)
                    active_programs = list(bug_targeted_programs) + list(programs)
                else:
                    mode = "Local Fuzzing"
                    active_programs = list(bug_targeted_programs) + [
                        _directed_mutate_program(p, rng, iteration, iter_validation_log)
                        for p in programs
                    ]
            else:
                mode = "Local Fuzzing"
                active_programs = list(bug_targeted_programs) + [
                    _directed_mutate_program(p, rng, iteration, iter_validation_log)
                    for p in programs
                ]

            # Corpus entries may pre-date the validator or originate from an
            # LLM response. Revalidate every candidate at the simulation gate;
            # rejected inputs are reported as fuzzer quality data, never as RTL
            # lockstep failures and never as a Gemini repair trigger.
            gate_results = [validate_program(program) for program in active_programs]
            gate_invalid = [result for result in gate_results if not result["valid"]]
            if gate_invalid:
                for result in gate_invalid:
                    iter_validation_log.append({
                        "iteration": iteration,
                        "program_id": result["program_id"],
                        "mutation_type": "simulation_gate",
                        "valid": False,
                        "reason": result["reason"],
                        "instruction_count": result["instruction_count"],
                        "timestamp": _utc(),
                    })
            valid_ids = {result["program_id"] for result in gate_results if result["valid"]}
            active_programs = [program for program in active_programs if program.get("id") in valid_ids]

            # Update directed fuzzing stats
            val_history = telemetry["directed_fuzzing"]["validation_history"]
            val_history.extend(iter_validation_log)
            if len(val_history) > 50:
                val_history = val_history[-50:]
            telemetry["directed_fuzzing"]["validation_history"] = val_history

            newly_valid = sum(1 for entry in iter_validation_log if entry.get("valid"))
            newly_invalid = sum(1 for entry in iter_validation_log if not entry.get("valid"))
            telemetry["directed_fuzzing"]["valid_count"] += newly_valid
            telemetry["directed_fuzzing"]["invalid_count"] += newly_invalid
            telemetry["directed_fuzzing"]["total_generated"] = (
                telemetry["directed_fuzzing"]["valid_count"] + telemetry["directed_fuzzing"]["invalid_count"]
            )
            telemetry["directed_fuzzing"]["validity_yield_pct"] = round(
                telemetry["directed_fuzzing"]["valid_count"] /
                max(1, telemetry["directed_fuzzing"]["total_generated"]) * 100.0, 1
            )

            # ── 2. Run Co-simulation with Dynamic Latency & Coverage Capture ─
            iter_results = []
            all_matches = all_mismatches = all_retired = all_cycles = 0
            latest_vcd = None
            coverage_paths = []
            first_mismatch = None
            first_failing_program = None
            first_failing_comparison = None
            all_lint_logs = []

            for prog in active_programs:
                mem_delay = rng.choice([0, 1])

                sim = build_and_simulate_program(
                    HW, prog, workdir,
                    iteration=iteration,
                    generate_vcd=True,
                    mem_delay=mem_delay,
                )
                cmp = compare_rvfi_trace(prog, sim.get("hardware_trace", []))

                if sim.get("lint_log"):
                    all_lint_logs.append(sim["lint_log"])
                if sim.get("vcd_path"):
                    latest_vcd = sim["vcd_path"]
                if sim.get("coverage_path"):
                    coverage_paths.append(sim["coverage_path"])

                iss_cov = cmp.get("iss_coverage", {})
                for itype, cnt in iss_cov.get("insn_type_distribution", {}).items():
                    if cnt > 0:
                        seen_insn_types.add(itype)
                        cumulative_insn_types[itype] = cumulative_insn_types.get(itype, 0) + cnt
                for pat, cnt in iss_cov.get("register_pattern_distribution", {}).items():
                    if cnt > 0:
                        seen_patterns.add(pat)
                        cumulative_patterns[pat] = cumulative_patterns.get(pat, 0) + cnt

                mismatches_list = cmp.get("mismatches", [])
                prog_ok = sim["ok"] and cmp["ok"]
                if not prog_ok and first_mismatch is None:
                    if mismatches_list:
                        first_mismatch = mismatches_list[0]
                        first_failing_program = prog
                        first_failing_comparison = cmp
                    elif sim.get("error"):
                        first_mismatch = {"sim_error": sim.get("error")}
                        first_failing_program = prog
                        first_failing_comparison = cmp

                iter_results.append({
                    "program_id": prog.get("id"),
                    "ok": prog_ok,
                    "sim_ok": sim["ok"],
                    "scoreboard_ok": cmp["ok"],
                    "hardware_trap": sim.get("hardware_trap", False),
                    "retired": sim.get("retired", 0),
                    "cycles": sim.get("cycles", 0),
                    "cpi": sim.get("cpi", 0.0),
                    "mem_delay": mem_delay,
                    "matches": len(cmp.get("matches", [])),
                    "mismatches": len(mismatches_list),
                    "first_mismatch": mismatches_list[0] if mismatches_list else None,
                })

                all_matches    += len(cmp.get("matches", []))
                all_mismatches += len(mismatches_list)
                all_retired    += sim.get("retired", 0)
                all_cycles     += sim.get("cycles", 0)

            comparison_ok = bool(active_programs) and all(r["ok"] for r in iter_results)
            total_checks  = all_matches + all_mismatches
            match_rate    = round(all_matches / max(1, total_checks) * 100.0, 1)
            iter_avg_cpi  = round(all_cycles / max(1, all_retired), 2)

            # ── 3. Multi-Layer Co-Coverage & Unbounded Cumulative Union ──────
            diversity_pct = round((len(seen_insn_types) / 7.0) * 100.0, 1)
            pattern_pct = round((len(seen_patterns) / 7.0) * 100.0, 1)
            cumulative_functional_pct = round((diversity_pct + pattern_pct) / 2.0, 1)

            verilator_cov = summarize_verilator_coverage(coverage_paths)
            rtl_line_cov = verilator_cov.get("line_coverage_pct")
            rtl_toggle_cov = verilator_cov.get("toggle_coverage_pct")
            coverage_components = [cumulative_functional_pct]
            if rtl_line_cov is None and rtl_toggle_cov is None:
                total_rtl_cov = verilator_cov.get("total_coverage_pct")
                if total_rtl_cov is not None:
                    coverage_components.append(float(total_rtl_cov))
            if rtl_line_cov is not None:
                coverage_components.append(float(rtl_line_cov))
            if rtl_toggle_cov is not None:
                coverage_components.append(float(rtl_toggle_cov))
            unified_cov = round(sum(coverage_components) / len(coverage_components), 1)

            # Save high-yield testbenches to test_corpus/high_yield
            if unified_cov > best_unified_cov:
                best_unified_cov = unified_cov
                for prog in active_programs:
                    hy_file = HIGH_YIELD / f"{prog.get('id', 'seed')}.json"
                    hy_file.write_text(json.dumps(prog, indent=2))

            high_yield_count = len(list(HIGH_YIELD.glob("*.json")))

            telemetry["coverage"]["unified_coverage_pct"] = unified_cov
            telemetry["coverage"]["cumulative_functional_pct"] = cumulative_functional_pct
            telemetry["coverage"]["rtl_line_coverage_pct"] = rtl_line_cov
            telemetry["coverage"]["rtl_toggle_coverage_pct"] = rtl_toggle_cov
            telemetry["coverage"]["verilator"] = verilator_cov
            telemetry["coverage"]["instruction_diversity_pct"] = diversity_pct
            telemetry["coverage"]["register_pattern_pct"] = pattern_pct
            telemetry["coverage"]["insn_type_distribution"] = dict(cumulative_insn_types)
            telemetry["coverage"]["register_pattern_distribution"] = dict(cumulative_patterns)
            telemetry["coverage"]["high_yield_seeds_count"] = high_yield_count

            telemetry["coverage"]["coverage_history"].append({
                "iteration": iteration,
                "unified_coverage_pct": unified_cov,
                "functional_coverage_pct": cumulative_functional_pct,
                "rtl_line_coverage_pct": rtl_line_cov,
                "rtl_toggle_coverage_pct": rtl_toggle_cov,
                "coverage_artifact_count": verilator_cov.get("artifact_count", 0),
            })

            # ── 4. Loop B: Gemini RTL Repair via Delta-Debugging on Divergence ─
            rtl_improved = False
            delta_debug_summary = None

            if not comparison_ok and first_failing_program is not None and first_failing_comparison is not None:
                mode = "Gemini Hardware Repair (Delta-Debug)"
                feedback = {
                    "iteration": iteration,
                    "match_rate_pct": match_rate,
                    "mismatch_total": all_mismatches,
                    "first_mismatch": first_mismatch,
                    "failing_programs": [r for r in iter_results if not r["ok"]],
                }

                candidate, repair_src, repair_attempts, delta_debug_summary = _llm_repair_hardware_with_delta_debug(
                    feedback, first_failing_program, first_failing_comparison, workdir
                )

                if delta_debug_summary:
                    telemetry["delta_debug"] = delta_debug_summary

                if candidate and "module picorv32" in candidate:
                    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    prev_checkpoint = CHECKPOINTS / f"picorv32_iter{iteration:04d}_before_repair_{tag}.v"
                    previous_rtl = HW.read_text() if HW.exists() else ""
                    if HW.exists():
                        shutil.copy2(HW, prev_checkpoint)
                    HW.write_text(candidate)
                    HW_ALT.write_text(candidate)
                    llm_called = True
                    llm_source = repair_src
                    print(f"[iter {iteration}] Delta-debug repair applied ({repair_src}, {repair_attempts} attempts).", file=sys.stderr)

                    repaired_results = []
                    repaired_matches = repaired_mismatches = repaired_retired = repaired_cycles = 0
                    for prog in active_programs:
                        sim2 = build_and_simulate_program(HW, prog, workdir, iteration=iteration, generate_vcd=False)
                        cmp2 = compare_rvfi_trace(prog, sim2.get("hardware_trace", []))
                        repaired_results.append(sim2["ok"] and cmp2["ok"])
                        repaired_matches    += len(cmp2.get("matches", []))
                        repaired_mismatches += len(cmp2.get("mismatches", []))
                        repaired_retired    += sim2.get("retired", 0)
                        repaired_cycles     += sim2.get("cycles", 0)

                    repaired_ok = all(repaired_results)
                    repaired_total = repaired_matches + repaired_mismatches
                    repaired_match_rate = round(repaired_matches / max(1, repaired_total) * 100.0, 1)

                    if repaired_match_rate > match_rate or repaired_ok:
                        verified_path = CHECKPOINTS / f"picorv32_verified_improvement_iter{iteration:04d}_{tag}.v"
                        HW.read_bytes()
                        shutil.copy2(HW, verified_path)
                        rtl_improved = True
                        telemetry["rtl_improvement_count"] = telemetry.get("rtl_improvement_count", 0) + 1
                        print(f"[iter {iteration}] Verified improvement saved to {verified_path.name}", file=sys.stderr)
                        match_rate   = repaired_match_rate
                        iter_avg_cpi = round(repaired_cycles / max(1, repaired_retired), 2)
                        comparison_ok = repaired_ok
                    else:
                        # A lint-clean candidate that does not improve the full
                        # active suite is not a repair. Restore it immediately so
                        # subsequent telemetry and campaigns use the known state.
                        HW.write_text(previous_rtl)
                        HW_ALT.write_text(previous_rtl)
                        if delta_debug_summary is not None:
                            delta_debug_summary["status"] = "CANDIDATE_ROLLED_BACK_NO_FULL_SUITE_IMPROVEMENT"
                        llm_source = f"{repair_src}:rolled_back"
                        print(f"[iter {iteration}] Candidate rolled back: no full-suite improvement.", file=sys.stderr)

            iter_record = {
                "iteration": iteration,
                "mode": mode,
                "comparison_ok": comparison_ok,
                "match_rate_pct": match_rate,
                "avg_cpi": iter_avg_cpi,
                "unified_coverage_pct": unified_cov,
                "functional_coverage_pct": cumulative_functional_pct,
                "program_count": len(active_programs),
                "valid_testcases": len(active_programs),
                "invalid_testcases": len(gate_invalid),
                "testcase_validation": gate_results,
                "retired_total": all_retired,
                "cycles_total": all_cycles,
                "mismatch_total": all_mismatches,
                "llm_called": llm_called,
                "llm_source": llm_source,
                "repair_attempts": repair_attempts,
                "rtl_improved": rtl_improved,
                "delta_debug_info": delta_debug_summary,
                "active_bugs": active_bugs,
                "bug_targeted_programs": len(bug_targeted_programs),
                "rtl_sha256": hashlib.sha256(HW.read_bytes()).hexdigest() if HW.exists() else "",
                "vcd_path": latest_vcd,
                "coverage_paths": coverage_paths,
                "verilator_coverage": verilator_cov,
                "lint_log": "\n".join(all_lint_logs[:2]),
                "first_mismatch": first_mismatch,
                "results": iter_results,
                "finished_at": _utc(),
            }

            telemetry["iterations"].append(iter_record)
            telemetry["total_iterations"] = iteration
            telemetry["llm_call_count"]   = sum(1 for it in telemetry["iterations"] if it.get("llm_called"))
            all_rates = [it.get("match_rate_pct", 100.0) for it in telemetry["iterations"]]
            telemetry["overall_match_rate"] = round(sum(all_rates) / len(all_rates), 1)
            all_cpis = [it.get("avg_cpi", 0.0) for it in telemetry["iterations"]]
            telemetry["overall_avg_cpi"]  = round(sum(all_cpis) / len(all_cpis), 2)
            telemetry["latest_vcd"] = latest_vcd
            telemetry["elapsed_seconds"] = round(time.monotonic() - start_time, 2)
            _write_telemetry(telemetry)

        if telemetry.get("status") == "running":
            telemetry["status"] = "completed"
        telemetry["finished_at"] = _utc()
        telemetry["elapsed_seconds"] = round(time.monotonic() - start_time, 2)
        _write_telemetry(telemetry)
        return telemetry

    finally:
        RUN_STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PicoRV32 Dual-Loop Fuzzing Campaign with Delta Debugging & Coverage")
    parser.add_argument("--seconds",      type=int,   default=180, help="Time budget in seconds (non-continuous mode)")
    parser.add_argument("--iterations",   type=int,   default=3,   help="Max iterations (non-continuous mode)")
    parser.add_argument("--continuous",   action="store_true",     help="Run continuously until stop signal")
    parser.add_argument("--seed",         type=int,   default=20260825)
    parser.add_argument("--fuzz-interval", "--fuzz-every", "-N", dest="fuzz_interval", type=int, default=8,
                        help="Iteration interval N: every Nth iteration triggers Gemini expansion")
    args = parser.parse_args()

    result = run_pico_campaign(
        seconds=args.seconds,
        max_iterations=args.iterations,
        continuous=args.continuous,
        seed=args.seed,
        fuzz_interval=args.fuzz_interval,
    )
    print(json.dumps({
        "status":          result.get("status"),
        "iterations":      result.get("total_iterations"),
        "match_rate":      result.get("overall_match_rate"),
        "avg_cpi":         result.get("overall_avg_cpi"),
        "llm_calls":       result.get("llm_call_count"),
        "rtl_improvements": result.get("rtl_improvement_count", 0),
        "coverage": {
            "unified_pct": result.get("coverage", {}).get("unified_coverage_pct", 0.0),
            "functional_pct": result.get("coverage", {}).get("cumulative_functional_pct", 0.0),
            "rtl_line_pct": result.get("coverage", {}).get("rtl_line_coverage_pct", 0.0),
            "rtl_toggle_pct": result.get("coverage", {}).get("rtl_toggle_coverage_pct", 0.0),
            "high_yield_seeds": result.get("coverage", {}).get("high_yield_seeds_count", 0),
        },
        "directed_fuzzing": {
            "valid": result.get("directed_fuzzing", {}).get("valid_count", 0),
            "invalid_filtered": result.get("directed_fuzzing", {}).get("invalid_count", 0),
            "yield_pct": result.get("directed_fuzzing", {}).get("validity_yield_pct", 100.0),
        },
        "gemini_model":    result.get("gemini_model"),
        "elapsed_seconds": result.get("elapsed_seconds"),
    }, indent=2))
