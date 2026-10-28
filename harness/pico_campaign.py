"""PicoRV32 dual-loop fuzzing campaign: directed fuzzing, LLM stimulus expansion, delta-debugging and
guarded LLM block-patch repair, with cumulative coverage and honest telemetry.

See CHANGES.md for the list of logic fixes relative to the first prototype.
"""

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
_GEMINI_TURNS: dict[str, int] = {}
_REJECTED_NOTES: dict[tuple[str, str], str] = {}   # (rtl sha, subsystem) -> why the last candidate was rolled back
_GEMINI_MAX_TURNS = 8      # recreate the chat session so history (huge RTL prompts) cannot grow without bound

from harness.riscv_cosim import (
    HW_PARAMS,
    get_space_safe_tempdir,
    run_verilator_lint,
    estimate_yosys_stats,
    build_and_simulate_program,
    compare_rvfi_trace,
    summarize_verilator_coverage,
    validate_program,
)
from harness.delta_debugger import (
    slice_to_minimal_reproducer,
    ddmin_reproducer,
    prefix_program,
    AdaptiveVectorScaler,
    localize_rtl_fault,
    format_localized_repair_prompt,
    parse_block_patches,
    apply_block_patches,
)
from sw_reference.golden_rv32i import CoverageTracker

try:
    from hw_design.bug_injector import get_active_bugs, restore_golden as _restore_golden
    _BUG_INJECTOR_AVAILABLE = True
except ImportError:
    _BUG_INJECTOR_AVAILABLE = False

    def get_active_bugs() -> list:  # type: ignore[misc]
        return []

    def _restore_golden() -> bool:  # type: ignore[misc]
        return False

ROOT = Path(__file__).resolve().parents[1]
HW = ROOT / "hw_design" / "picorv32_mutable.sv"
HW_ALT = ROOT / "hw_design" / "picorv32.v"
HW_GOLDEN_SV = ROOT / "hw_design" / "picorv32_golden.sv"
SW_REF = ROOT / "sw_reference" / "golden_rv32i.py"
PROGRAMS = ROOT / "test_corpus" / "rv32i_programs.json"
PROGRAMS_ALT = ROOT / "test_corpus" / "riscv_instructions.json"
HIGH_YIELD = ROOT / "test_corpus" / "high_yield"
LOGS = ROOT / "logs"
TELEMETRY = LOGS / "pico_campaign_telemetry.json"
TELEMETRY_ALT = LOGS / "picorv32_telemetry.json"
CHECKPOINTS = LOGS / "checkpoints"
GOLDEN_CHECKPOINT = CHECKPOINTS / "picorv32_golden.v"
STOP_FILE = LOGS / "stop_campaign"
RUN_STATE = LOGS / "pico_campaign.pid"
PROCESS_LOG = LOGS / "dashboard_campaign.log"

# Experiment switches (environment):
#   PICO_REPAIR_MODE=localized|monolithic   localized block patch vs whole-file context (ablation E5)
#   PICO_GOLDEN_RECOVERY=1                  allow restoring the golden RTL when the LLM fails; such events are
#                                           reported as "golden_recovery" and NEVER counted as repairs.
REPAIR_MODE = os.environ.get("PICO_REPAIR_MODE", "localized")
ALLOW_GOLDEN_RECOVERY = os.environ.get("PICO_GOLDEN_RECOVERY", "0") == "1"
CORPUS_CAP = 6

DEFAULT_SEED_PROGRAMS = [
    {
        "id": "rv32i-seed-001-alu-edge",
        "description": "ALU arithmetic and logical operations with boundary immediates and shifts",
        "base": "0x00000000",
        "words": [
            "0x7ff00093", "0x80000113", "0x002081b3", "0x40208233", "0xfff0c293",
            "0x3ff16313", "0x0000f393", "0x00409413", "0x00245493", "0x40315513",
            "0x001125b3", "0x0020b633", "0x00000013",
        ],
    },
    {
        "id": "rv32i-seed-002-mem-deps",
        "description": "Memory store and load dependency chains (sw/lw/sh/lh/sb/lb)",
        "base": "0x00000000",
        "words": [
            "0x000000b7", "0x10008093", "0x05a00113", "0x0020a023", "0x0000a183",
            "0x12300213", "0x00409223", "0x00409283", "0x07f00313", "0x00608423",
            "0x00808383", "0x00518433", "0x00000013",
        ],
    },
    {
        "id": "rv32i-seed-003-branch-jump",
        "description": "Control flow branch resolution (beq/bne/blt) and jump target links (jal)",
        "base": "0x00000000",
        "words": [
            "0x00a00093", "0x01400113", "0x00208863", "0x00209463", "0x06300193",
            "0x00100193", "0x0020c463", "0x06300213", "0x00200213", "0x008002ef",
            "0x06300313", "0x00300313", "0x00000013",
        ],
    },
]
PINNED_SEED_IDS = {p["id"] for p in DEFAULT_SEED_PROGRAMS}


# ── Utility ──────────────────────────────────────────────────────────────────

def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_int(value) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def _rtl_hash() -> str:
    return hashlib.sha256(HW.read_bytes()).hexdigest() if HW.exists() else ""


def _get_gemini_client():
    """Authenticated Gemini client (60 s timeout) and ordered model list."""
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
    """Try each model in order. Chat sessions are recreated every few turns."""
    attempts = []
    for model in models:
        try:
            chat = _GEMINI_CHATS.get(model)
            if chat is None or _GEMINI_TURNS.get(model, 0) >= _GEMINI_MAX_TURNS:
                chat = client.chats.create(model=model)
                _GEMINI_CHATS[model] = chat
                _GEMINI_TURNS[model] = 0
            _GEMINI_TURNS[model] += 1
            response = chat.send_message(prompt)
            text = getattr(response, "text", None)
            if isinstance(text, str) and text.strip():
                attempts.append({"model": model, "ok": True})
                return text.strip(), model, attempts
            attempts.append({"model": model, "ok": False, "error": "empty response"})
        except Exception as exc:
            _GEMINI_CHATS.pop(model, None)
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


def _b(rs1: int, rs2: int, off: int, f3: int) -> int:
    off &= 0x1FFF
    return (((off >> 12) & 1) << 31) | (((off >> 5) & 0x3F) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | \
           ((f3 & 7) << 12) | (((off >> 1) & 0xF) << 8) | (((off >> 11) & 1) << 7) | 0x63


def _lui(rd: int, upper20: int) -> int:
    return ((upper20 & 0xFFFFF) << 12) | ((rd & 31) << 7) | 0x37


NOP = 0x00000013


def _prog(pid: str, desc: str, words: list[int]) -> dict:
    return {"id": pid, "description": desc, "base": "0x00000000", "words": [f"0x{w & 0xFFFFFFFF:08x}" for w in words]}


# ── Loop A: directed local fuzzer ────────────────────────────────────────────

def _generate_candidate_mutation(program: dict, rng: random.Random, iteration: int) -> tuple[dict, str]:
    """Randomised hazard / boundary mutations. Never touches RTL."""
    words = [_as_int(w) for w in program.get("words", [])]
    if not words:
        return program, "identity"

    mutated = list(words)
    mutation_type = rng.choice([
        "raw_hazard_chain", "load_use_hazard", "imm_boundary", "shift_boundary",
        "reg_alias", "branch_tight", "walking_bits",
    ])

    if mutation_type == "raw_hazard_chain" and len(mutated) >= 4:
        reg_a, reg_b, reg_c = (rng.randrange(1, 16) for _ in range(3))
        idx = rng.randrange(0, len(mutated) - 3)
        mutated[idx] = _i(rng.choice([-2048, -1, 1023, 2047]), 0, 0, reg_a)
        mutated[idx + 1] = _r(0, reg_a, reg_a, 0, reg_b)
        mutated[idx + 2] = _r(0, reg_a, reg_b, 1, reg_c)

    elif mutation_type == "load_use_hazard" and len(mutated) >= 3:
        base_reg, val_reg, dest_reg = 1, rng.randrange(2, 8), rng.randrange(8, 16)
        idx = rng.randrange(0, len(mutated) - 2)
        mutated[idx] = _s(0x40, val_reg, base_reg, 2)
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
            # clear rs1 [19:15] and rs2 [24:20] only (the prototype's mask also wiped funct3)
            mutated[idx] = (mutated[idx] & ~0x01FF8000) | (t << 20) | (t << 15)

    elif mutation_type == "branch_tight" and len(mutated) >= 2:
        idx = rng.randrange(0, len(mutated) - 1)
        r1, r2 = rng.randrange(1, 8), rng.randrange(1, 8)
        mutated[idx] = _b(r1, r2, 8, 0)

    elif mutation_type == "walking_bits" and len(mutated) >= 3:
        rd, shamt = rng.randrange(1, 16), rng.randrange(1, 31)
        idx = rng.randrange(0, len(mutated) - 2)
        mutated[idx] = _i(1, 0, 0, rd)
        mutated[idx + 1] = (shamt << 20) | (rd << 15) | (1 << 12) | (rd << 7) | 0x13

    if mutated[-1] != NOP:
        mutated.append(NOP)

    return _prog(f"{program['id']}-fuzz-k{iteration}",
                 f"Directed dynamic stimulus ({mutation_type}) of {program['id']}", mutated), mutation_type


def _directed_mutate_program(program: dict, rng: random.Random, iteration: int,
                             validation_log: list[dict], max_attempts: int = 10) -> dict:
    """Generate candidates, validate each, log valid and rejected ones."""
    for _ in range(max_attempts):
        cand, mtype = _generate_candidate_mutation(program, rng, iteration)
        res = validate_program(cand)
        validation_log.append({
            "iteration": iteration, "program_id": cand["id"], "mutation_type": mtype, "valid": res["valid"],
            "reason": res["reason"] if not res["valid"] else "Valid RV32IM sequence",
            "instruction_count": res["instruction_count"], "timestamp": _utc(),
        })
        if res["valid"]:
            return cand

    fb = _prog(f"{program['id']}-directed-fallback-k{iteration}", "Deterministic valid RAW hazard chain",
               [_i(100, 0, 0, 1), _i(200, 0, 0, 2), _r(0, 2, 1, 0, 3), _r(0x20, 1, 3, 0, 4), NOP])
    validation_log.append({"iteration": iteration, "program_id": fb["id"], "mutation_type": "directed_fallback",
                           "valid": True, "reason": "Guaranteed valid RV32I sequence",
                           "instruction_count": len(fb["words"]), "timestamp": _utc()})
    return fb


def _local_seed_mutator_fallback(programs: list[dict], iteration: int, rng: random.Random) -> dict:
    """Seeded (reproducible) fallback when Gemini is unavailable."""
    HIGH_YIELD.mkdir(parents=True, exist_ok=True)
    seed_pool = list(programs)
    for hf in sorted(HIGH_YIELD.glob("*.json")):
        try:
            seed_pool.append(json.loads(hf.read_text()))
        except Exception:
            pass
    base_prog = rng.choice(seed_pool) if seed_pool else DEFAULT_SEED_PROGRAMS[0]
    mutated = [_as_int(w) for w in base_prog.get("words", [])]
    if len(mutated) >= 4:
        r1, r2, r3 = (rng.randrange(1, 16) for _ in range(3))
        mutated[0] = _i(rng.choice([-2048, -1, 1, 1023, 2047]), 0, 0, r1)
        mutated[1] = _r(0, r1, r1, 0, r2)
        mutated[2] = _r(0x20, r2, r1, 0, r3)
    if mutated[-1] != NOP:
        mutated.append(NOP)
    return _prog(f"rv32i-seed-mut-exp-{iteration}",
                 "High-yield seed fallback mutation with boundary arithmetic and hazard chains", mutated)


# ── Bug-targeted directed stimulus ───────────────────────────────────────────

def _generate_bug_targeted_stimulus(bug_id: str, iteration: int = 0) -> dict | None:
    """Deterministic programs that exercise the exact trigger condition of each injected bug.

    (Three of the prototype's programs did not trigger their bug: the XOR->OR bug needs a *negative*
    operand, the BLT bug needs rs1 < 0 <= rs2, and the handshake bug needs a halfword store at offset 2.)
    """
    mk = {
        "approx_multiplier": lambda: [_lui(1, 1), _i(-1792, 1, 0, 1), _i(0xC0, 0, 0, 2),
                                      _r(1, 2, 1, 0, 3), _r(1, 2, 1, 1, 4), NOP],
        "alu_operator_xor_or": lambda: [_i(15, 0, 0, 1), _i(-1, 1, 4, 2), _r(0, 1, 2, 4, 3), NOP],
        "branch_inversion_blt": lambda: [_i(-1, 0, 0, 1), _i(10, 0, 0, 2), _b(1, 2, 8, 4),
                                         _i(99, 0, 0, 3), _i(1, 0, 0, 4), NOP],
        "writeback_forwarding_drop": lambda: [_i(42, 0, 0, 5), _i(1, 5, 0, 6), _i(1, 6, 0, 7),
                                              _r(0, 7, 6, 0, 8), NOP],
        "mem_handshake_drop": lambda: [_i(0x100, 0, 0, 1), _i(10, 0, 0, 2), _s(2, 2, 1, 1),
                                       _i(2, 1, 1, 3, opcode=0x03), _s(2, 2, 1, 1), NOP],
        # ---- suite v2 (hw_design/bug_manifest_v2.json) ----
        "and_to_or_alu": lambda: [_i(0x0F, 0, 0, 1), _i(0x3C, 0, 0, 2), _r(0, 2, 1, 7, 3), _i(0x33, 1, 7, 4), NOP],
        # every R-type the core can hand to the PCPI bus (MUL*, DIV*, REM*) plus plain R-type ALU ops around them
        "fast_mul_decode_and_to_or": lambda: [_i(7, 0, 0, 1), _i(-3, 0, 0, 2)] + [_r(1, 2, 1, f3, 3 + f3) for f3 in range(8)]
                                             + [_r(0, 2, 1, 0, 11), _r(0x20, 2, 1, 0, 12), _r(0, 2, 1, 7, 13), NOP],
        "fast_mul_rs2_signed_widened": lambda: [_i(2, 0, 0, 1), _i(-1, 0, 0, 2), _r(1, 2, 1, 2, 3), _r(1, 2, 1, 3, 4), NOP],
        "store_after_store_dropped_intermittent": lambda: [_i(0x100, 0, 0, 1), _i(0x11, 0, 0, 2)]
                                                          + [_s(4 * k, 2, 1, 2) for k in range(12)]
                                                          + [_i(4 * k, 1, 2, 3 + k % 4, opcode=0x03) for k in range(12)] + [NOP],
        "mem_addr_bit2_stuck": lambda: [_i(0x100, 0, 0, 1), _i(0x5A, 0, 0, 2), _s(4, 2, 1, 2), _s(0, 0, 1, 2),
                                        _i(4, 1, 2, 3, opcode=0x03), _i(0, 1, 2, 4, opcode=0x03), NOP],
        "slt_signed_as_unsigned": lambda: [_i(-1, 0, 0, 1), _i(1, 0, 0, 2), _r(0, 2, 1, 2, 3), _b(1, 2, 8, 4),
                                           _i(5, 0, 0, 4), NOP],
        "beq_inverted": lambda: [_i(7, 0, 0, 1), _i(7, 0, 0, 2), _b(1, 2, 8, 0), _i(99, 0, 0, 3), _i(1, 0, 0, 4), NOP],
        "lui_auipc_rd_x0": lambda: [_lui(1, 0x12345), (1 << 12) | (2 << 7) | 0x17, _i(1, 1, 0, 3), _r(0, 2, 1, 0, 4), NOP],
        "store_after_store_dropped": lambda: [_i(0x100, 0, 0, 1), _i(0x11, 0, 0, 2), _s(0, 2, 1, 2), _s(4, 2, 1, 2),
                                              _s(8, 2, 1, 2), _i(4, 1, 2, 3, opcode=0x03), NOP],
        "stale_latched_branch": lambda: [_i(1, 0, 0, 1), _i(1, 0, 0, 2), _b(1, 2, 8, 0), _i(9, 0, 0, 9),
                                         _i(3, 0, 0, 3), _i(4, 3, 0, 4), _i(5, 4, 0, 5), NOP],
    }.get(bug_id)
    if mk is None:
        return None
    prog = _prog(f"bug-targeted-{bug_id}", f"Deterministic stimulus targeting {bug_id}", mk())
    return prog


# ── Gemini stimulus expansion ────────────────────────────────────────────────

def _build_gemini_expansion_context(programs: list[dict], telemetry: dict) -> str:
    cov = telemetry.get("coverage", {})
    insn_dist = cov.get("insn_type_distribution", {})
    pat_dist = cov.get("register_pattern_distribution", {})
    missing_insns = [t for t in CoverageTracker.INSN_TYPES if insn_dist.get(t, 0) == 0]
    missing_pats = [p for p in CoverageTracker.REGISTER_PATTERNS if pat_dist.get(p, 0) == 0]
    return f"""=== COVERAGE STATE ===
- Functional coverage (cumulative): {cov.get('cumulative_functional_pct', 0.0)}%
- RTL line coverage (cumulative): {cov.get('rtl_line_coverage_pct', 'N/A')}%
- RTL toggle coverage (cumulative): {cov.get('rtl_toggle_coverage_pct', 'N/A')}%
- Instruction categories never executed: {missing_insns or 'none'}
- Register value patterns never produced: {missing_pats or 'none'}

=== CURRENT CORPUS EXCERPT ===
{json.dumps(programs[-3:], indent=2)}
"""


def _merge_corpus(programs: list[dict], new_prog: dict) -> list[dict]:
    """Keep the hand-written seeds pinned; evict only the oldest generated programs."""
    pinned = [p for p in programs if p.get("id") in PINNED_SEED_IDS]
    others = [p for p in programs if p.get("id") not in PINNED_SEED_IDS] + [new_prog]
    return pinned + others[-max(1, CORPUS_CAP - len(pinned)):]


def _expand_testbench_with_gemini(programs: list[dict], iteration: int, telemetry: dict,
                                  validation_log: list[dict], rng: random.Random) -> tuple[list[dict], bool, str, bool]:
    """Returns (programs, program_added, source, gemini_called).

    Gemini gets one retry with the validator's rejection reason; the seeded local fallback is retried with
    different mutations until one passes the validator (a single draw failed ~50% of the time in the field).
    ``telemetry["gemini_expansion"]`` records attempts, acceptances, per-model counts and normalised rejection reasons.
    """
    client, models = _get_gemini_client()
    gemini_called = False
    stats = telemetry.setdefault("gemini_expansion", {"attempts": 0, "accepted": 0, "rejected": 0,
                                                      "api_failures": 0, "reject_reasons": {}})

    def count_reject(reason: str) -> None:
        stats["rejected"] += 1
        key = re.sub(r"0x[0-9a-fA-F]+|\d+", "N", reason)[:80]
        stats["reject_reasons"][key] = stats["reject_reasons"].get(key, 0) + 1

    if client is not None:
        gemini_called = True
        context = _build_gemini_expansion_context(programs, telemetry)
        insn_dist = telemetry.get("coverage", {}).get("insn_type_distribution", {})
        mul_nudge = ""
        if insn_dist.get("MUL_DIV", 0) == 0:
            mul_nudge = ("\n5. PRIORITY: MUL_DIV coverage is zero. Include MUL/MULH/MULHU/DIV/REM instructions "
                         "(opcode 0x33, funct7=0x01) with operands loaded via LUI/ADDI first "
                         "(e.g. rs1=0x900, rs2=0xC0).\n")
        base_prompt = f"""You are an expert RISC-V processor verification engineer performing target-guided hardware fuzzing.

{context}
=== GENERATION DIRECTIVE ===
Generate ONE RV32IM test program of 10 to 18 instructions that targets the deficits above:
1. Tight branch conditions (BEQ, BNE, BLT, BGE) including negative operands
2. Byte/halfword loads and stores (LB, LH, SB, SH) at naturally aligned addresses
3. Walking-ones (0x00000001 -> 0x80000000) via SLLI
4. Arithmetic boundaries (-2048, -1, 0, 2047)
{mul_nudge}
Hard constraints (programs violating them are discarded):
- Only RV32I base + RV32M. No CSR/system/fence/compressed instructions.
- Data addresses must be naturally aligned, lie in 0x100..0xFF00, and must NOT overlap the program image.
  Load a base register with ADDI/LUI first.
- Branches/jumps may only go forward and must land inside the program or exactly at its end.
- The program must terminate by falling through; the last instruction must be NOP (0x00000013).

Return ONLY a JSON object (no markdown):
{{"id": "rv32i-gemini-exp-{iteration}", "description": "...", "base": "0x00000000", "words": ["0x...", "..."]}}
"""
        last_reason = ""
        for _attempt in range(2):
            prompt = base_prompt + (f"\nYour previous program was rejected: {last_reason}\nFix that and answer again.\n"
                                    if last_reason else "")
            stats["attempts"] += 1
            used_model = None
            try:
                text, used_model, _ = _generate_with_gemini(client, models, prompt)
                if not text:
                    stats["api_failures"] += 1
                    break
                text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                data = json.loads(text)
                data.setdefault("id", f"rv32i-gemini-exp-{iteration}")
                data["base"] = "0x00000000"
                if not (isinstance(data.get("words"), list) and len(data["words"]) >= 4):
                    raise ValueError("fewer than 4 words")
                res = validate_program(data)
            except Exception as exc:
                last_reason = f"unusable answer ({type(exc).__name__}: {exc})"
                count_reject(last_reason)
                bm = stats.setdefault("by_model", {}).setdefault(str(used_model), {"accepted": 0, "rejected": 0})
                bm["rejected"] += 1
                continue
            validation_log.append({
                "iteration": iteration, "program_id": data["id"], "mutation_type": "gemini_expansion",
                "valid": res["valid"], "reason": res["reason"] or "Valid RV32IM sequence",
                "instruction_count": res["instruction_count"], "timestamp": _utc()})
            bm = stats.setdefault("by_model", {}).setdefault(str(used_model), {"accepted": 0, "rejected": 0})
            bm["accepted" if res["valid"] else "rejected"] += 1
            if res["valid"]:
                stats["accepted"] += 1
                return _merge_corpus(programs, data), True, f"gemini:{used_model}", True
            last_reason = res["reason"]
            count_reject(last_reason)

    for _try in range(10):
        fb = _local_seed_mutator_fallback(programs, iteration, rng)
        res = validate_program(fb)
        validation_log.append({"iteration": iteration, "program_id": fb["id"], "mutation_type": "seed_mutator_fallback",
                               "valid": res["valid"], "reason": res["reason"] or "Valid RV32IM sequence",
                               "instruction_count": res["instruction_count"], "timestamp": _utc()})
        if res["valid"]:
            return _merge_corpus(programs, fb), True, "local_seed_mutator_fallback", gemini_called
    return programs, False, "none", gemini_called


# ── Loop B: delta-debug + guarded block-patch repair ─────────────────────────

def _golden_text() -> str | None:
    for p in (HW_GOLDEN_SV, GOLDEN_CHECKPOINT):
        if p.exists():
            return p.read_text()
    return None


def _llm_repair_hardware_with_delta_debug(
    feedback: dict, failing_program: dict, comparison_result: dict, workdir: Path, max_retries: int = 3,
) -> tuple[str | None, str, int, dict]:
    """Returns (candidate_rtl | None, source, attempts, delta_debug_info).

    ``source`` starts with "gemini_" for LLM patches and "golden_recovery_" for a restored golden file; the
    latter is *not* a repair and must never be counted as one.
    """
    mem_delay = int(feedback.get("mem_delay", 0))
    validator = lambda p: validate_program(p)["valid"]
    first_mm = (comparison_result.get("mismatches") or [{}])[0]
    info = {"active": True, "original_program_id": failing_program.get("id"), "repair_mode": REPAIR_MODE,
            "failure_index_k": first_mm.get("retire_order", 0), "shrink_phases": [], "grow_phases": [],
            "status": "in_progress", "failing_instruction_disasm": first_mm.get("insn_disasm", ""),
            "failing_instruction_hex": first_mm.get("insn_hex", ""),
            "original_length": len(failing_program.get("words", []))}

    def recovery(tag: str, attempts: int):
        info["status"] = f"REPAIR_FAILED_{tag.upper()}"
        text = _golden_text() if ALLOW_GOLDEN_RECOVERY else None
        if text:
            return text, f"golden_recovery_{tag}", attempts, info
        return None, tag, attempts, info

    client, models = _get_gemini_client()
    if client is None:
        return recovery("no_api_key", 0)

    def sim_cmp(prog: dict, rtl: Path, wd: Path):
        sim = build_and_simulate_program(rtl, prog, wd, iteration=0, generate_vcd=False, mem_delay=mem_delay)
        cmp = compare_rvfi_trace(prog, sim.get("hardware_trace", []), hardware_trap=sim.get("hardware_trap", False))
        return sim, cmp

    def fails(prog: dict) -> bool:
        sim, cmp = sim_cmp(prog, HW, workdir)
        return bool(sim["ok"] and cmp["mismatches"])

    # 1. slice, verify it reproduces on the *unrepaired* RTL, 2. genuinely minimise
    reproducer, K, state = slice_to_minimal_reproducer(failing_program, comparison_result, True, validator)
    K = K or 0
    info["failure_index_k"] = K
    info["slice_mode"] = reproducer.get("metadata", {}).get("mode", "original")
    reproduces = fails(reproducer)
    info["slice_reproduces_failure"] = reproduces
    if not reproduces:
        reproducer = failing_program
    else:
        reproducer, dd_log = ddmin_reproducer(reproducer, fails)
        info["shrink_phases"] = dd_log
    info["minimal_reproducer_id"] = reproducer.get("id")
    info["minimal_program_length"] = len(reproducer.get("words", []))

    _, repro_cmp = sim_cmp(reproducer, HW, workdir)
    record = (repro_cmp.get("mismatches") or [first_mm])[0]
    info["failing_instruction_disasm"] = record.get("insn_disasm", info["failing_instruction_disasm"])
    info["failing_instruction_hex"] = record.get("insn_hex", info["failing_instruction_hex"])

    # 3. localisation
    rtl = HW.read_text(errors="ignore")
    total = len(rtl.splitlines())
    if REPAIR_MODE == "monolithic":
        localization = {"subsystem": "whole_file", "start_line": 1, "end_line": total, "diagnosis": "Whole file.",
                        "total_snippet_lines": total,
                        "candidates": [{"subsystem": "whole_file", "start_line": 1, "end_line": total,
                                        "diagnosis": "Entire RTL file.",
                                        "numbered_snippet": "\n".join(f"{n + 1:4d}: {l}" for n, l in enumerate(rtl.splitlines()))}]}
    else:
        localization = localize_rtl_fault(rtl, record)
    allowed = [(c["start_line"], c["end_line"]) for c in localization["candidates"]]
    info["localized_subsystem"] = localization["subsystem"]
    info["localized_lines"] = allowed
    rkey = (hashlib.sha256(rtl.encode()).hexdigest(), localization["subsystem"])
    print(f"[pico_campaign] Localised to {localization['subsystem']} {allowed}", file=sys.stderr)

    cand_dir = workdir / "candidate"
    cand_dir.mkdir(exist_ok=True)
    last_error, attempts = "", 0
    for attempt in range(1, max_retries + 1):
        attempts = attempt
        prompt = format_localized_repair_prompt(reproducer, record, localization, state or None)
        if rkey in _REJECTED_NOTES:
            prompt += f"\n\nAn earlier candidate for this same RTL was rolled back: {_REJECTED_NOTES[rkey]}"
        if last_error:
            prompt += f"\n\nYour previous patch was rejected:\n{last_error}\nFix this and answer again."
        try:
            text, used_model, _ = _generate_with_gemini(client, models, prompt)
            if text is None:
                last_error = "model returned no answer"
                continue
            patches = parse_block_patches(text, allowed[0])
            candidate = apply_block_patches(rtl, patches, allowed)
        except ValueError as exc:
            last_error = f"patch error: {exc}"
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        lint_ok, lint_log = run_verilator_lint(rtl_content=candidate, workdir=workdir)
        if not lint_ok:
            last_error = "Verilator lint failed:\n" + lint_log[:2500]
            continue

        cand_rtl = cand_dir / "candidate.sv"
        cand_rtl.write_text(candidate)
        sim, cmp = sim_cmp(reproducer, cand_rtl, cand_dir)
        if not (sim["ok"] and cmp["ok"]):
            last_error = "candidate still fails the minimal reproducer: " + json.dumps(
                (cmp.get("mismatches") or [{"error": sim.get("error")}])[0].get("differences")
                or (cmp.get("mismatches") or [{}])[0].get("error") or sim.get("error"), default=str)[:600]
            continue
        info["grow_phases"].append({"phase": "minimal_reproducer", "ok": True})

        scaler = AdaptiveVectorScaler(len(failing_program.get("words", [])))
        scaler.start_grow()
        all_ok = True
        pct = scaler.next_grow_size()
        while pct is not None:
            pre = prefix_program(failing_program, K, pct)
            gsim, gcmp = sim_cmp(pre, cand_rtl, cand_dir)
            ok = bool(gsim["ok"] and gcmp["ok"])
            info["grow_phases"].append({"grow_percent": pct, "ok": ok})
            if not ok:
                all_ok = False
                last_error = f"regression on {pct}% prefix"
                break
            pct = scaler.next_grow_size()
        if all_ok:
            info["status"] = "VERIFIED_REGRESSION_FREE"
            info["repair_attempts"] = attempts
            return candidate, f"gemini_block_patch:{used_model}", attempts, info

    return recovery("exhausted", attempts)


# ── Campaign reset / telemetry ───────────────────────────────────────────────

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

    # one source of truth for "golden": hw_design/picorv32_golden.sv (same file the bug injector restores from)
    restored = False
    try:
        restored = bool(_restore_golden())
    except FileNotFoundError:
        pass
    if not restored and GOLDEN_CHECKPOINT.exists():
        shutil.copy2(GOLDEN_CHECKPOINT, HW)
    if HW.exists():
        shutil.copyfile(HW, HW_ALT)


def _init_telemetry(continuous: bool, seconds: int, seed: int, fuzz_interval: int) -> dict:
    LOGS.mkdir(exist_ok=True)
    CHECKPOINTS.mkdir(exist_ok=True)
    HIGH_YIELD.mkdir(parents=True, exist_ok=True)

    if TELEMETRY.exists():
        history = LOGS / "history"
        history.mkdir(exist_ok=True)
        tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(TELEMETRY, history / f"telemetry_{tag}.json")

    t = {
        "campaign_id": uuid.uuid4().hex[:12],
        "benchmark": "picorv32_processor_cosim",
        "started_at": _utc(),
        "status": "running",
        "continuous": continuous,
        "timeout_seconds": None if continuous else seconds,
        "seed": seed,
        "fuzz_interval": fuzz_interval,
        "repair_mode": REPAIR_MODE,
        "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        "gemini_fallback_model": os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite"),
        "gemini_api_key_present": bool(os.environ.get("GEMINI_API_KEY", "")),
        "hardware_target": str(HW),
        "hardware_config": {k: bool(v) for k, v in HW_PARAMS.items()},   # the real Verilator build flags
        "total_iterations": 0,
        "llm_call_count": 0,
        "rtl_improvement_count": 0,
        "golden_recovery_count": 0,
        "overall_match_rate": 100.0,
        "overall_avg_cpi": 0.0,
        "latest_vcd": None,
        "yosys_stats": {"total_cells": 0, "wires": 0, "memories": 0},
        "directed_fuzzing": {"total_generated": 0, "valid_count": 0, "invalid_count": 0,
                             "validity_yield_pct": 100.0, "validation_history": []},
        "coverage": {"unified_coverage_pct": 0.0, "cumulative_functional_pct": 0.0, "rtl_line_coverage_pct": None,
                     "rtl_toggle_coverage_pct": None, "instruction_diversity_pct": 0.0,
                     "register_pattern_pct": 0.0, "insn_type_distribution": {},
                     "register_pattern_distribution": {}, "high_yield_seeds_count": 0, "coverage_history": []},
        "delta_debug": {"active": False, "failure_index_k": None, "failing_instruction": None,
                        "failing_pc": None, "status": "idle"},
        "iterations": [],
    }
    _write_telemetry(t)
    return t


def _write_telemetry(t: dict) -> None:
    data = json.dumps(t, indent=2, default=str)
    for path in (TELEMETRY, TELEMETRY_ALT):
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tf:
            tf.write(data)
            temp_name = tf.name
        os.replace(temp_name, path)


def _load_corpus() -> list[dict]:
    if not PROGRAMS.exists():
        PROGRAMS.parent.mkdir(parents=True, exist_ok=True)
        PROGRAMS.write_text(json.dumps(DEFAULT_SEED_PROGRAMS, indent=2) + "\n")
    programs = json.loads(PROGRAMS.read_text())
    have = {p.get("id") for p in programs}
    return [p for p in DEFAULT_SEED_PROGRAMS if p["id"] not in have] + programs   # seeds always present


def _save_corpus(programs: list[dict]) -> None:
    text = json.dumps(programs, indent=2) + "\n"
    PROGRAMS.write_text(text)
    PROGRAMS_ALT.write_text(text)


# ── Main campaign ────────────────────────────────────────────────────────────

def run_pico_campaign(seconds: int = 180, max_iterations: int | None = 3, continuous: bool = False,
                      seed: int = 20260825, fuzz_interval: int = 8) -> dict:
    STOP_FILE.unlink(missing_ok=True)
    fuzz_interval = max(1, int(fuzz_interval))
    telemetry = _init_telemetry(continuous, seconds, seed, fuzz_interval)
    RUN_STATE.write_text(str(os.getpid()))

    if not HW.exists() or HW.stat().st_size < 1000:
        try:
            _restore_golden()
        except FileNotFoundError:
            if GOLDEN_CHECKPOINT.exists():
                shutil.copy2(GOLDEN_CHECKPOINT, HW)

    programs = _load_corpus()
    rng = random.Random(seed)
    workdir = get_space_safe_tempdir()

    ys = estimate_yosys_stats(HW, workdir)
    telemetry["yosys_stats"] = {k: ys.get(k, 0) for k in ("total_cells", "wires", "memories")}
    telemetry["yosys_stats"]["note"] = "" if ys.get("total_cells") else (ys.get("log") or "")[-300:]
    _write_telemetry(telemetry)

    start_time = time.monotonic()
    iteration = 0
    seen_insn_types: set[str] = set()
    seen_patterns: set[str] = set()
    cumulative_insn_types: dict[str, int] = {}
    cumulative_patterns: dict[str, int] = {}
    rtl_cumulative: dict[str, int] = {}
    cov_rtl_hash = _rtl_hash()
    best_unified_cov = 0.0
    n_insn_types = len(CoverageTracker.INSN_TYPES)
    n_patterns = len(CoverageTracker.REGISTER_PATTERNS)

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
            iter_validation_log: list[dict] = []

            active_bugs = get_active_bugs()
            bug_programs = [p for p in (_generate_bug_targeted_stimulus(b, iteration) for b in active_bugs) if p]

            # ── 1. stimulus ──────────────────────────────────────────────
            mode = "Local Fuzzing"
            active_programs = None
            if is_expansion_iter:
                mode = "Gemini Testbench Expansion"
                expanded, added, llm_source, gemini_called = _expand_testbench_with_gemini(
                    programs, iteration, telemetry, iter_validation_log, rng)
                llm_called = gemini_called
                if added:
                    programs = expanded
                    _save_corpus(programs)
                    active_programs = bug_programs + list(programs)
                else:
                    mode = "Local Fuzzing"
            if active_programs is None:
                active_programs = bug_programs + [_directed_mutate_program(p, rng, iteration, iter_validation_log)
                                                  for p in programs]

            gate_results = [validate_program(p) for p in active_programs]
            gate_invalid = [r for r in gate_results if not r["valid"]]
            for r in gate_invalid:
                iter_validation_log.append({"iteration": iteration, "program_id": r["program_id"],
                                            "mutation_type": "simulation_gate", "valid": False,
                                            "reason": r["reason"], "instruction_count": r["instruction_count"],
                                            "timestamp": _utc()})
            active_programs = [p for p, r in zip(active_programs, gate_results) if r["valid"]]   # index-based

            df = telemetry["directed_fuzzing"]
            df["validation_history"] = (df["validation_history"] + iter_validation_log)[-50:]
            df["valid_count"] += sum(1 for e in iter_validation_log if e.get("valid"))
            df["invalid_count"] += sum(1 for e in iter_validation_log if not e.get("valid"))
            df["total_generated"] = df["valid_count"] + df["invalid_count"]
            df["validity_yield_pct"] = round(df["valid_count"] / max(1, df["total_generated"]) * 100.0, 1)

            # ── 2. co-simulation ─────────────────────────────────────────
            iter_results = []
            all_matches = all_mismatches = all_retired = all_cycles = 0
            latest_vcd = None
            coverage_paths = []
            first_mismatch = first_failing_program = first_failing_comparison = None
            first_fail_delay = 0
            harness_errors = []
            all_lint_logs = []

            for n, prog in enumerate(active_programs):
                mem_delay = rng.choice([0, 1])
                sim = build_and_simulate_program(HW, prog, workdir, iteration=iteration, mem_delay=mem_delay,
                                                 generate_vcd=(n == 0), vcd_name="picorv32_latest.vcd")
                cmp = compare_rvfi_trace(prog, sim.get("hardware_trace", []),
                                         hardware_trap=sim.get("hardware_trap", False))
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
                prog_ok = bool(sim["ok"] and cmp["ok"])
                if not sim["ok"]:
                    harness_errors.append({"program_id": prog.get("id"), "error": sim.get("error")})
                elif mismatches_list and first_mismatch is None:
                    first_mismatch, first_failing_program = mismatches_list[0], prog
                    first_failing_comparison, first_fail_delay = cmp, mem_delay

                iter_results.append({
                    "program_id": prog.get("id"), "ok": prog_ok, "sim_ok": sim["ok"], "scoreboard_ok": cmp["ok"],
                    "harness_error": None if sim["ok"] else sim.get("error"),
                    "hardware_trap": sim.get("hardware_trap", False), "retired": sim.get("retired", 0),
                    "cycles": sim.get("cycles", 0), "cpi": sim.get("cpi", 0.0), "mem_delay": mem_delay,
                    "matches": len(cmp.get("matches", [])), "mismatches": len(mismatches_list),
                    "first_mismatch": mismatches_list[0] if mismatches_list else None,
                })
                all_matches += len(cmp.get("matches", []))
                all_mismatches += len(mismatches_list)
                all_retired += sim.get("retired", 0)
                all_cycles += sim.get("cycles", 0)

            comparison_ok = bool(active_programs) and all(r["ok"] for r in iter_results)
            bug_programs_failing = [r["program_id"] for r in iter_results
                                    if str(r["program_id"]).startswith("bug-targeted-") and not r["ok"]]
            match_rate = round(all_matches / max(1, all_matches + all_mismatches) * 100.0, 1)
            iter_avg_cpi = round(all_cycles / max(1, all_retired), 2)

            # ── 3. cumulative coverage ───────────────────────────────────
            diversity_pct = round(len(seen_insn_types) / n_insn_types * 100.0, 1)
            pattern_pct = round(len(seen_patterns) / n_patterns * 100.0, 1)
            cumulative_functional_pct = round((diversity_pct + pattern_pct) / 2.0, 1)

            verilator_cov = summarize_verilator_coverage(coverage_paths, rtl_cumulative)
            rtl_line, rtl_toggle = verilator_cov.get("line_coverage_pct"), verilator_cov.get("toggle_coverage_pct")
            components = [cumulative_functional_pct] + [float(x) for x in (rtl_line, rtl_toggle) if x is not None]
            unified_cov = round(sum(components) / len(components), 1)

            if unified_cov > best_unified_cov:
                best_unified_cov = unified_cov
                for prog in active_programs:
                    (HIGH_YIELD / f"{prog.get('id', 'seed')}.json").write_text(json.dumps(prog, indent=2))

            cov = telemetry["coverage"]
            cov.update(unified_coverage_pct=unified_cov, cumulative_functional_pct=cumulative_functional_pct,
                       rtl_line_coverage_pct=rtl_line, rtl_toggle_coverage_pct=rtl_toggle, verilator=verilator_cov,
                       instruction_diversity_pct=diversity_pct, register_pattern_pct=pattern_pct,
                       insn_type_distribution=dict(cumulative_insn_types),
                       register_pattern_distribution=dict(cumulative_patterns),
                       high_yield_seeds_count=len(list(HIGH_YIELD.glob("*.json"))))
            cov["coverage_history"].append({
                "iteration": iteration, "unified_coverage_pct": unified_cov,
                "functional_coverage_pct": cumulative_functional_pct, "rtl_line_coverage_pct": rtl_line,
                "rtl_toggle_coverage_pct": rtl_toggle, "coverage_artifact_count": verilator_cov.get("artifact_count", 0)})

            # ── 4. repair (only for genuine scoreboard mismatches) ───────
            rtl_improved = golden_recovered = False
            delta_debug_summary = None
            if first_failing_program is not None:
                mode = "Gemini Hardware Repair (Delta-Debug)"
                before_ok = [r["ok"] for r in iter_results]
                fail_sim = build_and_simulate_program(HW, first_failing_program, workdir, iteration=iteration,
                                                      generate_vcd=True, mem_delay=first_fail_delay,
                                                      vcd_name=f"picorv32_fail_iter{iteration:04d}.vcd")
                latest_vcd = fail_sim.get("vcd_path") or latest_vcd
                feedback = {"iteration": iteration, "match_rate_pct": match_rate, "mem_delay": first_fail_delay}
                candidate, repair_src, repair_attempts, delta_debug_summary = _llm_repair_hardware_with_delta_debug(
                    feedback, first_failing_program, first_failing_comparison, workdir)
                telemetry["delta_debug"] = delta_debug_summary
                llm_called = True
                llm_source = repair_src

                if candidate and "module picorv32" in candidate:
                    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    previous_rtl = HW.read_text() if HW.exists() else ""
                    if HW.exists():
                        shutil.copy2(HW, CHECKPOINTS / f"picorv32_iter{iteration:04d}_before_repair_{tag}.v")
                    HW.write_text(candidate)
                    HW_ALT.write_text(candidate)

                    after_ok, rep_matches, rep_mism, rep_ret, rep_cyc = [], 0, 0, 0, 0
                    regressed: list[dict] = []
                    for idx, prog in enumerate(active_programs):
                        # same memory-delay as the "before" run, else delay-sensitive programs look like regressions
                        s2 = build_and_simulate_program(HW, prog, workdir, iteration=iteration, generate_vcd=False,
                                                        mem_delay=iter_results[idx]["mem_delay"])
                        c2 = compare_rvfi_trace(prog, s2.get("hardware_trace", []), hardware_trap=s2.get("hardware_trap", False))
                        after_ok.append(bool(s2["ok"] and c2["ok"]))
                        if before_ok[len(after_ok) - 1] and not after_ok[-1]:
                            mm0 = (c2.get("mismatches") or [{}])[0]
                            regressed.append({"program_id": prog.get("id"), "kind": mm0.get("kind") or s2.get("error"),
                                              "insn": mm0.get("insn_disasm"),
                                              "fields": sorted((mm0.get("differences") or {}).keys())})
                        rep_matches += len(c2.get("matches", []))
                        rep_mism += len(c2.get("mismatches", []))
                        rep_ret += s2.get("retired", 0)
                        rep_cyc += s2.get("cycles", 0)

                    no_regression = all(a for b, a in zip(before_ok, after_ok) if b)
                    fewer_failures = after_ok.count(False) < before_ok.count(False)
                    if no_regression and fewer_failures:
                        if repair_src.startswith("golden_recovery"):
                            golden_recovered = True
                            telemetry["golden_recovery_count"] += 1
                        else:
                            rtl_improved = True
                            telemetry["rtl_improvement_count"] += 1
                            shutil.copy2(HW, CHECKPOINTS / f"picorv32_verified_improvement_iter{iteration:04d}_{tag}.v")
                        match_rate = round(rep_matches / max(1, rep_matches + rep_mism) * 100.0, 1)
                        iter_avg_cpi = round(rep_cyc / max(1, rep_ret), 2)
                        comparison_ok = all(after_ok)
                    else:
                        HW.write_text(previous_rtl)
                        HW_ALT.write_text(previous_rtl)
                        delta_debug_summary["rollback"] = {"regressed_programs": regressed,
                                                           "failing_before": before_ok.count(False),
                                                           "failing_after": after_ok.count(False)}
                        if regressed:
                            _REJECTED_NOTES[(hashlib.sha256(previous_rtl.encode()).hexdigest(),
                                             delta_debug_summary.get("localized_subsystem", ""))] = (
                                "it passed the reproducer but made previously passing programs fail: "
                                + json.dumps(regressed[:3], default=str) + ". Do not change behaviour outside the faulty statement.")
                        delta_debug_summary["status"] = "CANDIDATE_ROLLED_BACK_NO_FULL_SUITE_IMPROVEMENT"
                        llm_source = f"{repair_src}:rolled_back"
                        print(f"[iter {iteration}] Candidate rolled back (regression={not no_regression}).", file=sys.stderr)

            new_hash = _rtl_hash()
            if new_hash != cov_rtl_hash:      # coverage points move when the RTL changes; restart the union
                rtl_cumulative.clear()
                cov_rtl_hash = new_hash

            iter_record = {
                "iteration": iteration, "mode": mode, "comparison_ok": comparison_ok,
                "match_rate_pct": match_rate, "avg_cpi": iter_avg_cpi, "unified_coverage_pct": unified_cov,
                "functional_coverage_pct": cumulative_functional_pct, "program_count": len(active_programs),
                "valid_testcases": len(active_programs), "invalid_testcases": len(gate_invalid),
                "testcase_validation": gate_results, "retired_total": all_retired, "cycles_total": all_cycles,
                "mismatch_total": all_mismatches, "harness_errors": harness_errors,
                "llm_called": llm_called, "llm_source": llm_source, "repair_attempts": repair_attempts,
                "rtl_improved": rtl_improved, "golden_recovered": golden_recovered,
                "delta_debug_info": delta_debug_summary, "active_bugs": active_bugs,
                "bug_programs_failing": bug_programs_failing,
                "bug_targeted_programs": len(bug_programs), "rtl_sha256": new_hash, "vcd_path": latest_vcd,
                "verilator_coverage": verilator_cov, "lint_log": "\n".join(all_lint_logs[:2]),
                "first_mismatch": first_mismatch, "results": iter_results, "finished_at": _utc(),
            }
            telemetry["iterations"].append(iter_record)
            telemetry["total_iterations"] = iteration
            telemetry["llm_call_count"] = sum(1 for it in telemetry["iterations"] if it.get("llm_called"))
            telemetry["overall_match_rate"] = round(sum(it["match_rate_pct"] for it in telemetry["iterations"])
                                                    / len(telemetry["iterations"]), 1)
            telemetry["overall_avg_cpi"] = round(sum(it["avg_cpi"] for it in telemetry["iterations"])
                                                 / len(telemetry["iterations"]), 2)
            telemetry["latest_vcd"] = latest_vcd
            telemetry["elapsed_seconds"] = round(time.monotonic() - start_time, 2)
            _write_telemetry(telemetry)

        if telemetry.get("status") == "running":
            telemetry["status"] = "completed"
    except Exception as exc:                      # never leave telemetry stuck in "running"
        import traceback
        telemetry["status"] = "crashed"
        telemetry["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        telemetry["finished_at"] = _utc()
        telemetry["elapsed_seconds"] = round(time.monotonic() - start_time, 2)
        _write_telemetry(telemetry)
        RUN_STATE.unlink(missing_ok=True)
    return telemetry


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PicoRV32 dual-loop fuzzing campaign")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--fuzz-interval", "--fuzz-every", "-N", dest="fuzz_interval", type=int, default=8)
    args = parser.parse_args()

    result = run_pico_campaign(seconds=args.seconds, max_iterations=args.iterations, continuous=args.continuous,
                               seed=args.seed, fuzz_interval=args.fuzz_interval)
    cv = result.get("coverage", {})
    print(json.dumps({
        "status": result.get("status"), "iterations": result.get("total_iterations"),
        "match_rate": result.get("overall_match_rate"), "avg_cpi": result.get("overall_avg_cpi"),
        "llm_calls": result.get("llm_call_count"), "rtl_improvements": result.get("rtl_improvement_count", 0),
        "golden_recoveries": result.get("golden_recovery_count", 0),
        "coverage": {"unified_pct": cv.get("unified_coverage_pct"), "functional_pct": cv.get("cumulative_functional_pct"),
                     "rtl_line_pct": cv.get("rtl_line_coverage_pct"), "rtl_toggle_pct": cv.get("rtl_toggle_coverage_pct"),
                     "high_yield_seeds": cv.get("high_yield_seeds_count")},
        "directed_fuzzing": {"valid": result.get("directed_fuzzing", {}).get("valid_count", 0),
                             "invalid_filtered": result.get("directed_fuzzing", {}).get("invalid_count", 0),
                             "yield_pct": result.get("directed_fuzzing", {}).get("validity_yield_pct", 100.0)},
        "gemini_model": result.get("gemini_model"), "elapsed_seconds": result.get("elapsed_seconds"),
    }, indent=2))
