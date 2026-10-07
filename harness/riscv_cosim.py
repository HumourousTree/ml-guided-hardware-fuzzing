"""Verilator dynamic C++ harness builder, Yosys cell estimator, and RVFI lockstep scoreboard."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from sw_reference.golden_rv32i import RV32IReference, CoverageTracker
from harness.delta_debugger import disassemble_rv32i

RVFI_FIELDS = (
    "pc_rdata",
    "pc_wdata",
    "insn",
    "rd_addr",
    "rd_wdata",
    "mem_addr",
    "mem_rmask",
    "mem_wmask",
    "mem_rdata",
    "mem_wdata",
)


def _as_int(value) -> int:
    if isinstance(value, str):
        return int(value, 0)
    return int(value) if value is not None else 0


def _u32(value: int) -> int:
    """Mask to 32 bits."""
    return value & 0xFFFFFFFF


def validate_instruction_sequence(words: list[int], base: int = 0) -> tuple[bool, str]:
    """Reject unsupported or unaligned RV32I encodings before they reach Verilator."""
    if not words:
        return False, "program contains no instructions"
    # Strict base RV32I opcodes only
    supported_opcodes = {0x03, 0x13, 0x17, 0x23, 0x33, 0x37, 0x63, 0x67, 0x6F}
    for index, word in enumerate(words):
        insn, opcode, funct3, funct7 = _u32(word), _u32(word) & 0x7F, (_u32(word) >> 12) & 7, (_u32(word) >> 25) & 0x7F
        if opcode not in supported_opcodes:
            return False, f"instruction {index}: unsupported opcode 0x{opcode:02x}"
        if opcode == 0x03 and funct3 not in {0, 1, 2, 4, 5}:
            return False, f"instruction {index}: invalid load funct3 {funct3}"
        if opcode == 0x23 and funct3 not in {0, 1, 2}:
            return False, f"instruction {index}: invalid store funct3 {funct3}"
        if opcode == 0x63 and funct3 not in {0, 1, 4, 5, 6, 7}:
            return False, f"instruction {index}: invalid branch funct3 {funct3}"
        if opcode == 0x67 and funct3 != 0:
            return False, f"instruction {index}: invalid JALR funct3 {funct3}"
        if opcode == 0x13 and funct3 == 1 and funct7 != 0:
            return False, f"instruction {index}: invalid SLLI encoding"
        if opcode == 0x13 and funct3 == 5 and funct7 not in {0, 0x20}:
            return False, f"instruction {index}: invalid SRLI/SRAI encoding"
        if opcode == 0x33:
            if funct7 == 0x01:
                # RV32M standard extension (mul, mulh, mulhsu, mulhu, div, divu, rem, remu)
                if funct3 not in range(8):
                    return False, f"instruction {index}: invalid RV32M funct3 {funct3}"
            else:
                if funct3 == 0 and funct7 not in {0, 0x20}:
                    return False, f"instruction {index}: invalid ADD/SUB encoding"
                if funct3 == 5 and funct7 not in {0, 0x20}:
                    return False, f"instruction {index}: invalid SRL/SRA encoding"
                if funct3 not in {0, 1, 2, 3, 4, 5, 6, 7}:
                    return False, f"instruction {index}: invalid OP funct3 {funct3}"

    reference = RV32IReference([_u32(word) for word in words], base)
    program_end, steps = base + 4 * len(words), 0
    try:
        while base <= reference.pc < program_end and steps < len(words):
            event = reference.step()
            insn, address = event["insn"], event["mem_addr"]
            opcode, funct3 = insn & 0x7F, (insn >> 12) & 7
            access_size = {0: 1, 1: 2, 2: 4, 4: 1, 5: 2}.get(funct3) if opcode == 0x03 else {0: 1, 1: 2, 2: 4}.get(funct3)
            if opcode in {0x03, 0x23} and access_size and address % access_size:
                return False, f"instruction {steps}: unaligned {access_size}-byte memory access at 0x{address:08x}"
            steps += 1
    except ValueError as exc:
        return False, str(exc)
    return True, ""


def validate_program(program: dict) -> dict:
    """Structured validation result suitable for campaign telemetry and UI."""
    try:
        words = [_as_int(word) for word in program.get("words", [])]
        valid, reason = validate_instruction_sequence(words, _as_int(program.get("base", 0)))
    except (TypeError, ValueError) as exc:
        valid, reason = False, f"invalid instruction encoding: {exc}"
    return {
        "program_id": program.get("id", "unnamed"),
        "valid": valid,
        "reason": reason,
        "instruction_count": len(program.get("words", [])),
    }


def get_space_safe_tempdir(prefix: str = "benchfuzz-picorv32-") -> Path:
    """Create a space-safe temporary root directory (e.g., /tmp/benchfuzz-picorv32-<id>)."""
    base_tmp = "/tmp" if os.path.exists("/tmp") and os.access("/tmp", os.W_OK) else tempfile.gettempdir()
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=base_tmp))
    return path


def run_verilator_lint(rtl_content: str | None = None, rtl_path: Path | None = None, workdir: Path | None = None) -> tuple[bool, str]:
    """Run Verilator lint check on RTL. Returns (is_clean, diagnostics_log)."""
    cleanup = False
    if workdir is None:
        workdir = get_space_safe_tempdir("benchfuzz-lint-")
        cleanup = True

    try:
        target_v = workdir / "picorv32.v"
        if rtl_content is not None:
            target_v.write_text(rtl_content)
        elif rtl_path is not None:
            shutil.copy2(rtl_path, target_v)
        else:
            return False, "No RTL content or path provided."

        cmd = [
            "verilator",
            "--lint-only",
            "-Wall",
            "-Wno-fatal",
            "-Wno-DECLFILENAME",
            "-Wno-MULTITOP",
            "-Wno-BLKSEQ",
            "-Wno-GENUNNAMED",
            "-Wno-PINMISSING",
            "-Wno-WIDTH",
            "-Wno-CASEOVERLAP",
            "-Wno-CASEINCOMPLETE",
            "-Wno-UNUSED",
            "-DRISCV_FORMAL",
            "--top-module",
            "picorv32",
            "-GREGS_INIT_ZERO=1",
            "-GENABLE_FAST_MUL=1",
            "picorv32.v",
        ]
        res = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
        log = (res.stdout + "\n" + res.stderr).strip()
        ok = res.returncode == 0 and "%Error" not in log
        return ok, log
    except FileNotFoundError:
        return False, "verilator executable not found in PATH."
    except Exception as exc:
        return False, f"Verilator lint execution error: {exc}"
    finally:
        if cleanup and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)


def estimate_yosys_stats(rtl_path: Path, workdir: Path | None = None) -> dict:
    """Run fast Yosys synthesis hierarchy analysis to extract cell counts and logic complexity."""
    cleanup = False
    if workdir is None:
        workdir = get_space_safe_tempdir("benchfuzz-yosys-")
        cleanup = True

    stats = {"cells": {}, "total_cells": 0, "wires": 0, "memories": 0, "log": ""}
    try:
        local_rtl = workdir / "picorv32.v"
        shutil.copy2(rtl_path, local_rtl)
        script = f"""
read_verilog -sv picorv32.v
hierarchy -check -top picorv32
proc
opt
stat -json
"""
        script_file = workdir / "synth_stats.ys"
        script_file.write_text(script)
        res = subprocess.run(["yosys", "-s", str(script_file)], cwd=workdir, capture_output=True, text=True)
        stats["log"] = (res.stdout + "\n" + res.stderr).strip()
        if res.returncode == 0:
            output = res.stdout + "\n" + res.stderr
            cells_match = re.search(r'"num_cells":\s*(\d+)', output)
            wires_match = re.search(r'"num_wires":\s*(\d+)', output)
            if cells_match:
                stats["total_cells"] = int(cells_match.group(1))
            if wires_match:
                stats["wires"] = int(wires_match.group(1))
            for cell, count in re.findall(r"\|\s*(\S+)\s*\|\s*(\d+)\s*\|", output):
                if cell not in {"Number", "Cell"}:
                    stats["cells"][cell] = int(count)
            stats["memories"] = sum(
                count for name, count in stats["cells"].items()
                if "MEM" in name.upper()
            )
    except Exception as exc:
        stats["log"] = f"Yosys estimation unavailable: {exc}"
    finally:
        if cleanup and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
    return stats


def summarize_verilator_coverage(coverage_paths: list[str | Path]) -> dict:
    """Best-effort summary of real Verilator coverage artifacts.

    Verilator's coverage database is intentionally retained as the source of
    truth. If the optional ``verilator_coverage`` executable is unavailable,
    the dashboard reports the artifact count instead of inventing percentages.
    """
    paths = [Path(path) for path in coverage_paths if path and Path(path).exists()]
    summary = {
        "artifact_count": len(paths),
        "available": bool(paths),
        "tool_available": shutil.which("verilator_coverage") is not None,
        "line_coverage_pct": None,
        "toggle_coverage_pct": None,
        "total_coverage_pct": None,
        "annotated_path": None,
    }
    if not paths or not summary["tool_available"]:
        return summary

    annotate_dir = paths[0].parent / "coverage_annotated"
    annotate_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        ["verilator_coverage", "--annotate", str(annotate_dir), *[path.name for path in paths]],
        cwd=paths[0].parent,
        capture_output=True,
        text=True,
    )
    summary["annotated_path"] = str(annotate_dir)
    summary["log"] = (result.stdout + "\n" + result.stderr).strip()
    annotated_counts = []
    for annotated_file in annotate_dir.rglob("*"):
        if annotated_file.is_file():
            try:
                annotated_counts.extend(
                    int(match.group(1))
                    for line in annotated_file.read_text(errors="ignore").splitlines()
                    if (match := re.match(r"^%(\d+)\s", line))
                )
            except OSError:
                continue
    if annotated_counts:
        summary["line_coverage_pct"] = round(
            100.0 * sum(count > 0 for count in annotated_counts) / len(annotated_counts), 2
        )
    total_match = re.search(r"Total coverage\s+\(\d+/\d+\)\s+(\d+(?:\.\d+)?)%", summary["log"])
    if total_match:
        summary["total_coverage_pct"] = float(total_match.group(1))
    percentages = [
        float(value)
        for value in re.findall(r"(?:line|toggle)[^0-9]*(\d+(?:\.\d+)?)\s*%", summary["log"], re.IGNORECASE)
    ]
    if percentages:
        if len(percentages) >= 1:
            summary["line_coverage_pct"] = percentages[0]
        if len(percentages) >= 2:
            summary["toggle_coverage_pct"] = percentages[1]
    return summary


CPP_HARNESS_TEMPLATE = """#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <cstdint>
#include <iomanip>
#include <cstring>
#include <cstdlib>
#include "Vpicorv32.h"
#include "verilated.h"
#if VM_TRACE
#include "verilated_vcd_c.h"
#endif
#if VM_COVERAGE
#include "verilated_cov.h"
#endif

static const uint32_t MEM_SIZE = 64 * 1024;
static uint8_t memory[MEM_SIZE] = {0};

static uint32_t mem_read_u32(uint32_t addr) {
    addr = addr & (MEM_SIZE - 1);
    return (uint32_t)memory[addr] |
           ((uint32_t)memory[(addr + 1) & (MEM_SIZE - 1)] << 8) |
           ((uint32_t)memory[(addr + 2) & (MEM_SIZE - 1)] << 16) |
           ((uint32_t)memory[(addr + 3) & (MEM_SIZE - 1)] << 24);
}

static void mem_write(uint32_t addr, uint32_t data, uint8_t strb) {
    addr = addr & (MEM_SIZE - 1);
    if (strb & 1) memory[addr] = data & 0xFF;
    if (strb & 2) memory[(addr + 1) & (MEM_SIZE - 1)] = (data >> 8) & 0xFF;
    if (strb & 4) memory[(addr + 2) & (MEM_SIZE - 1)] = (data >> 16) & 0xFF;
    if (strb & 8) memory[(addr + 3) & (MEM_SIZE - 1)] = (data >> 24) & 0xFF;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    std::string hex_path = (argc > 1) ? argv[1] : "";
    std::string vcd_path = (argc > 2) ? argv[2] : "";
    std::string jsonl_path = (argc > 3) ? argv[3] : "";
    int mem_delay_max = (argc > 4) ? std::atoi(argv[4]) : 0;
    std::string coverage_path = (argc > 5) ? argv[5] : "";

    std::ofstream trace_file;
    if (!jsonl_path.empty()) {
        trace_file.open(jsonl_path);
    }

    std::memset(memory, 0, sizeof(memory));

    int expected_total_retires = 0;
    if (!hex_path.empty()) {
        std::ifstream hf(hex_path);
        std::string line;
        uint32_t addr = 0;
        while (std::getline(hf, line)) {
            line.erase(0, line.find_first_not_of(" \\t\\r\\n"));
            line.erase(line.find_last_not_of(" \\t\\r\\n") + 1);
            if (line.empty() || line[0] == '#') continue;
            uint32_t word = std::stoul(line, nullptr, 16);
            mem_write(addr, word, 0xF);
            addr += 4;
            expected_total_retires++;
        }
    }

    Vpicorv32* top = new Vpicorv32;
#if VM_TRACE
    VerilatedVcdC* tfp = nullptr;
    if (!vcd_path.empty()) {
        Verilated::traceEverOn(true);
        tfp = new VerilatedVcdC;
        top->trace(tfp, 99);
        tfp->open(vcd_path.c_str());
    }
#endif

    top->clk = 0;
    top->resetn = 0;
    top->mem_ready = 0;
    top->mem_rdata = 0;
    top->irq = 0;

    vluint64_t main_time = 0;
    int total_cycles = 0;
    int retired = 0;
    int cycles_since_retire = 0;
    int mem_delay_counter = 0;
    const int max_cycles_without_retire = 150;
    const int max_total_cycles = std::max(500, expected_total_retires * 80);

    for (int cycle = 0; cycle < max_total_cycles; ++cycle) {
        if (cycle >= 10) top->resetn = 1;

        // Clock Low
        top->clk = 0;
        top->eval();
#if VM_TRACE
        if (tfp) tfp->dump(main_time);
#endif
        main_time += 5;

        // Handshake memory interface with dynamic wait states
        if (top->mem_valid) {
            if (mem_delay_counter > 0) {
                mem_delay_counter--;
                top->mem_ready = 0;
            } else {
                top->mem_ready = 1;
                if (top->mem_wstrb == 0) {
                    top->mem_rdata = mem_read_u32(top->mem_addr & ~3);
                } else {
                    mem_write(top->mem_addr & ~3, top->mem_wdata, top->mem_wstrb);
                    top->mem_rdata = 0;
                }
                // Re-arm delay for the NEXT transaction only after this one completes
                if (mem_delay_max > 0) {
                    mem_delay_counter = (std::rand() % (mem_delay_max + 1));
                }
            }
        } else {
            top->mem_ready = 0;
            // Do NOT re-arm delay counter when no transaction is in flight —
            // that would artificially inflate latency for unrelated transactions.
        }

        // Clock High
        top->clk = 1;
        top->eval();
#if VM_TRACE
        if (tfp) tfp->dump(main_time);
#endif
        main_time += 5;

        total_cycles++;
        cycles_since_retire++;

        if (top->resetn && top->trap) {
            std::string trap_record = "{\\"trap\\": true, \\"valid\\": false, \\"pc_rdata\\": " +
                                      std::to_string(top->rvfi_pc_rdata) +
                                      ", \\"cycles\\": " + std::to_string(total_cycles) +
                                      ", \\"reason\\": \\"hardware_trap_asserted\\"}";
            if (trace_file.is_open()) trace_file << trap_record << std::endl;
            std::cout << trap_record << std::endl;
            break;
        }

        if (top->resetn && top->rvfi_valid) {
            retired++;
            cycles_since_retire = 0;

            std::string record = "{\\"valid\\": true";
            record += ", \\"order\\": " + std::to_string(top->rvfi_order);
            record += ", \\"pc_rdata\\": " + std::to_string(top->rvfi_pc_rdata);
            record += ", \\"pc_wdata\\": " + std::to_string(top->rvfi_pc_wdata);
            record += ", \\"insn\\": " + std::to_string(top->rvfi_insn);
            record += ", \\"rd_addr\\": " + std::to_string((int)top->rvfi_rd_addr);
            record += ", \\"rd_wdata\\": " + std::to_string(top->rvfi_rd_wdata);
            record += ", \\"mem_addr\\": " + std::to_string(top->rvfi_mem_addr);
            record += ", \\"mem_rmask\\": " + std::to_string((int)top->rvfi_mem_rmask);
            record += ", \\"mem_wmask\\": " + std::to_string((int)top->rvfi_mem_wmask);
            record += ", \\"mem_rdata\\": " + std::to_string(top->rvfi_mem_rdata);
            record += ", \\"mem_wdata\\": " + std::to_string(top->rvfi_mem_wdata);
            record += ", \\"cycles\\": " + std::to_string(total_cycles);
            record += "}";

            if (trace_file.is_open()) trace_file << record << std::endl;
            std::cout << record << std::endl;
        }

        if (cycles_since_retire >= max_cycles_without_retire && retired > 0) break;
        if (retired >= expected_total_retires && cycles_since_retire >= 10) break;
    }

#if VM_TRACE
    if (tfp) { tfp->close(); delete tfp; }
#endif
#if VM_COVERAGE
    if (!coverage_path.empty()) {
        VerilatedCov::write(coverage_path.c_str());
    }
#endif
    if (trace_file.is_open()) trace_file.close();
    delete top;
    return 0;
}
"""


def _ensure_sim_binary(rtl_path: Path, workdir: Path) -> tuple[Path | None, str]:
    """Compile the reusable simulation binary if not yet built or if RTL changed."""
    target_v = workdir / "picorv32.v"
    sim_binary = workdir / "obj_dir" / "sim_picorv32"

    needs_compile = False
    if not target_v.exists() or target_v.stat().st_mtime < rtl_path.stat().st_mtime:
        shutil.copy2(rtl_path, target_v)
        needs_compile = True
    elif not sim_binary.exists():
        needs_compile = True

    if not needs_compile:
        return sim_binary, ""

    tb_path = workdir / "picorv32_harness.cpp"
    tb_path.write_text(CPP_HARNESS_TEMPLATE)

    verilator_cmd = [
        "verilator",
        "-Wall",
        "-Wno-fatal",
        "-Wno-DECLFILENAME",
        "-Wno-MULTITOP",
        "-Wno-BLKSEQ",
        "-Wno-GENUNNAMED",
        "-Wno-PINMISSING",
        "-Wno-WIDTH",
        "-Wno-CASEOVERLAP",
        "-Wno-CASEINCOMPLETE",
        "-Wno-UNUSED",
        "-DRISCV_FORMAL",
        "--top-module",
        "picorv32",
        "-GREGS_INIT_ZERO=1",
        "-GENABLE_FAST_MUL=1",
        "--cc",
        "picorv32.v",
        "--exe",
        "picorv32_harness.cpp",
        "--trace",
        "--coverage",
        "--coverage-line",
        "--coverage-toggle",
        "--coverage-user",
        "-o",
        "sim_picorv32",
    ]

    verilator_res = subprocess.run(verilator_cmd, cwd=workdir, capture_output=True, text=True)
    lint_log = (verilator_res.stdout + "\n" + verilator_res.stderr).strip()
    if verilator_res.returncode != 0 or "%Error" in lint_log:
        return None, lint_log

    make_cmd = ["make", "-C", "obj_dir", "-f", "Vpicorv32.mk", "-j4"]
    make_res = subprocess.run(make_cmd, cwd=workdir, capture_output=True, text=True)
    if make_res.returncode != 0:
        return None, (make_res.stdout + "\n" + make_res.stderr).strip()

    if not sim_binary.exists():
        sim_binary = workdir / "sim_picorv32"

    return (sim_binary if sim_binary.exists() else None), lint_log


def build_and_simulate_program(
    rtl_path: Path,
    program: dict,
    workdir: Path,
    iteration: int = 0,
    generate_vcd: bool = True,
    mem_delay: int = 0,
) -> dict:
    """Simulate a program against PicoRV32 Verilog RTL using a fast reusable binary."""
    words = [_as_int(w) for w in program.get("words", [])]

    validation = validate_program(program)
    if not validation["valid"]:
        return {
            "ok": False,
            "error": f"Instruction validation failed: {validation['reason']}",
            "lint_log": "",
            "hardware_trace": [],
            "retired": 0,
            "cycles": 0,
            "cpi": 0.0,
            "vcd_path": None,
            "coverage_path": None,
            "validation": validation,
        }

    sim_binary, lint_log = _ensure_sim_binary(rtl_path, workdir)
    if sim_binary is None:
        return {
            "ok": False,
            "error": "Simulator binary compilation failed",
            "lint_log": lint_log,
            "hardware_trace": [],
            "retired": 0,
            "cycles": 0,
            "cpi": 0.0,
            "vcd_path": None,
            "coverage_path": None,
            "validation": validation,
        }

    # Write program image to hex file
    prog_id = program.get("id", f"iter{iteration:04d}")
    hex_path = workdir / f"{prog_id}.hex"
    hex_lines = [f"{w & 0xFFFFFFFF:08x}" for w in words]
    hex_path.write_text("\n".join(hex_lines) + "\n")

    vcd_path = workdir / f"picorv32_iter{iteration:04d}.vcd" if generate_vcd else ""
    jsonl_path = workdir / f"{prog_id}.jsonl"
    coverage_path = workdir / f"{prog_id}.coverage.dat"

    exec_args = [
        str(sim_binary),
        str(hex_path),
        str(vcd_path) if vcd_path else "",
        str(jsonl_path),
        str(mem_delay),
        str(coverage_path),
    ]

    try:
        sim_res = subprocess.run(exec_args, cwd=workdir, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "simulation timed out after 15 seconds",
            "lint_log": lint_log,
            "hardware_trace": [],
            "retired": 0,
            "cycles": 0,
            "cpi": 0.0,
            "vcd_path": None,
            "validation": validation,
        }

    hardware_trace = []
    hardware_trap = False

    for line in sim_res.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                rec = json.loads(line)
                if rec.get("trap"):
                    hardware_trap = True
                elif rec.get("valid"):
                    hardware_trace.append(rec)
            except Exception:
                pass

    total_cycles = hardware_trace[-1].get("cycles", 0) if hardware_trace else 0
    retired_count = len(hardware_trace)
    cpi = round(total_cycles / max(1, retired_count), 2)

    return {
        "ok": True,
        "lint_log": lint_log,
        "hardware_trace": hardware_trace,
        "hardware_trap": hardware_trap,
        "retired": retired_count,
        "cycles": total_cycles,
        "cpi": cpi,
        "vcd_path": str(vcd_path) if (vcd_path and Path(vcd_path).exists()) else None,
        "coverage_path": str(coverage_path) if coverage_path.exists() else None,
        "validation": validation,
        "mem_delay": mem_delay,
    }


def compare_rvfi_trace(
    program: dict,
    hardware_trace: list[dict],
    normalize_pc: bool = False,
) -> dict:
    """Compare hardware RVFI retire trace against immutable Python ISS in lockstep."""
    words = [_as_int(w) for w in program.get("words", [])]
    base = _as_int(program.get("base", 0))
    prog_end = base + len(words) * 4
    reference = RV32IReference(words, base)
    cov_tracker = CoverageTracker()

    mismatches = []
    matches = []
    hardware_pc_base = None

    for order, actual in enumerate(hardware_trace):
        if not actual.get("valid", False):
            continue
        if hardware_pc_base is None:
            hardware_pc_base = _as_int(actual.get("pc_rdata", 0))

        if reference.pc >= prog_end or reference.pc < base:
            break

        try:
            expected = reference.step()
        except ValueError as exc:
            insn_val = _as_int(actual.get("insn", 0))
            record = {
                "retire_order": order,
                "pc": actual.get("pc_rdata", 0),
                "insn_hex": f"0x{insn_val:08x}",
                "insn_disasm": disassemble_rv32i(insn_val),
                "expected": None,
                "actual": actual,
                "error": str(exc),
            }
            mismatches.append(record)
            break

        insn_val = _as_int(actual.get("insn", expected.get("insn", 0)))
        cov_tracker.sample_instruction(insn_val, expected.get("rd_wdata", 0))

        diffs = {}
        for field in ("pc_rdata", "pc_wdata", "insn", "rd_addr", "rd_wdata", "mem_addr", "mem_rmask", "mem_wmask"):
            hw_val = _as_int(actual.get(field, -1))
            sw_val = expected[field]
            if normalize_pc and field in {"pc_rdata", "pc_wdata"}:
                hw_val = _u32(hw_val - hardware_pc_base)
                sw_val = _u32(sw_val - base)
            if hw_val != sw_val:
                diffs[field] = {"expected": sw_val, "actual": hw_val}

        # Validate mem_wdata masked by mem_wmask
        mem_wmask = expected.get("mem_wmask", 0) & 0xF
        if mem_wmask != 0:
            hw_wdata = _as_int(actual.get("mem_wdata", -1))
            sw_wdata = expected.get("mem_wdata", 0)
            mask_32 = sum(0xFF << (8 * i) for i in range(4) if (mem_wmask & (1 << i)))
            if (hw_wdata & mask_32) != (sw_wdata & mask_32):
                diffs["mem_wdata"] = {"expected": sw_wdata & mask_32, "actual": hw_wdata & mask_32}

        # Validate mem_rdata masked by mem_rmask
        mem_rmask = expected.get("mem_rmask", 0) & 0xF
        if mem_rmask != 0:
            hw_rdata = _as_int(actual.get("mem_rdata", -1))
            sw_rdata = expected.get("mem_rdata", 0)
            mask_32 = sum(0xFF << (8 * i) for i in range(4) if (mem_rmask & (1 << i)))
            if (hw_rdata & mask_32) != (sw_rdata & mask_32):
                diffs["mem_rdata"] = {"expected": sw_rdata & mask_32, "actual": hw_rdata & mask_32}

        record = {
            "retire_order": order,
            "pc": actual.get("pc_rdata", 0),
            "insn_hex": f"0x{insn_val:08x}",
            "insn_disasm": disassemble_rv32i(insn_val),
            "expected": expected,
            "actual": actual,
        }

        if diffs:
            record["differences"] = diffs
            mismatches.append(record)
            break
        matches.append(record)

    total_checked = len(matches) + len(mismatches)
    match_rate = round(100.0 * len(matches) / max(1, total_checked), 2) if total_checked > 0 else 0.0

    return {
        "ok": len(mismatches) == 0 and len(matches) > 0,
        "match_rate": match_rate,
        "total_matched": len(matches),
        "total_checked": total_checked,
        "matches": matches,
        "mismatches": mismatches,
        "retired": len(matches),
        "missing_retires": max(0, len(words) - len(matches)),
        "iss_coverage": cov_tracker.get_summary(),
    }
