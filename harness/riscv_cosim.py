"""Verilator C++ harness builder, Yosys estimator, program validator and RVFI lockstep scoreboard.

Key behaviour changes versus the first prototype (details in CHANGES.md):

* The DUT configuration is a single source of truth (``HW_PARAMS``).  The
  prototype enabled the multiplier but not the divider while the validator,
  ISS and LLM prompt all allowed DIV/REM, so those programs trapped in hardware.
* The simulator is rebuilt when the *content hash* of the RTL changes (the old
  mtime test reused a stale binary when an older-mtime file replaced a newer one).
* The scoreboard detects missing retirements (hardware trapped / stopped early),
  extra retirements, and applies PicoRV32's aligned-``mem_addr`` RVFI convention.
* The validator checks every R-type funct7, alignment, the 64 KiB memory window,
  overlap with the program image, termination and the final PC.
* Verilator coverage is parsed from the ``.dat`` files and merged cumulatively.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from sw_reference.golden_rv32i import RV32IReference, CoverageTracker
from harness.delta_debugger import disassemble_rv32i

# ── Single source of truth for the DUT build ─────────────────────────────────
HW_PARAMS = {"ENABLE_FAST_MUL": 1, "ENABLE_DIV": 1, "REGS_INIT_ZERO": 1}
HW_MEM_SIZE = 64 * 1024            # must match MEM_SIZE in CPP_HARNESS_TEMPLATE
RVFI_ALIGNED_MEM = True            # PicoRV32 reports word-aligned rvfi_mem_addr (riscv-formal ALIGNED_MEM)

RVFI_FIELDS = ("pc_rdata", "pc_wdata", "insn", "rd_addr", "rd_wdata",
               "mem_addr", "mem_rmask", "mem_wmask", "mem_rdata", "mem_wdata")

_VERILATOR_WARN_FLAGS = [
    "-Wall", "-Wno-fatal", "-Wno-DECLFILENAME", "-Wno-MULTITOP", "-Wno-BLKSEQ", "-Wno-GENUNNAMED",
    "-Wno-PINMISSING", "-Wno-WIDTH", "-Wno-CASEOVERLAP", "-Wno-CASEINCOMPLETE", "-Wno-UNUSED",
]


def _param_flags() -> list[str]:
    return [f"-G{k}={v}" for k, v in HW_PARAMS.items()]


def _as_int(value) -> int:
    if isinstance(value, str):
        return int(value, 0)
    return int(value) if value is not None else 0


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


# ── Program validation ───────────────────────────────────────────────────────

_SUPPORTED_OPCODES = {0x03, 0x0F, 0x13, 0x17, 0x23, 0x33, 0x37, 0x63, 0x67, 0x6F}


def _static_check(index: int, word: int) -> str | None:
    opcode, funct3, funct7 = word & 0x7F, (word >> 12) & 7, (word >> 25) & 0x7F
    if opcode not in _SUPPORTED_OPCODES:
        return f"instruction {index}: unsupported opcode 0x{opcode:02x}"
    if opcode == 0x0F and funct3 != 0:
        return f"instruction {index}: only FENCE (funct3 0) is supported, not FENCE.I"
    if opcode == 0x03 and funct3 not in {0, 1, 2, 4, 5}:
        return f"instruction {index}: invalid load funct3 {funct3}"
    if opcode == 0x23 and funct3 not in {0, 1, 2}:
        return f"instruction {index}: invalid store funct3 {funct3}"
    if opcode == 0x63 and funct3 not in {0, 1, 4, 5, 6, 7}:
        return f"instruction {index}: invalid branch funct3 {funct3}"
    if opcode == 0x67 and funct3 != 0:
        return f"instruction {index}: invalid JALR funct3 {funct3}"
    if opcode == 0x13:
        if funct3 == 1 and funct7 != 0:
            return f"instruction {index}: invalid SLLI encoding"
        if funct3 == 5 and funct7 not in {0, 0x20}:
            return f"instruction {index}: invalid SRLI/SRAI encoding"
    if opcode == 0x33:
        if funct7 == 0x00:
            pass
        elif funct7 == 0x01:
            pass  # RV32M (hardware built with ENABLE_FAST_MUL and ENABLE_DIV)
        elif funct7 == 0x20 and funct3 in (0, 5):
            pass  # SUB / SRA
        else:
            return f"instruction {index}: invalid OP encoding funct7=0x{funct7:02x} funct3={funct3}"
    return None


def analyze_program(words: list[int], base: int = 0) -> dict:
    """Static + dynamic validation. Returns {'ok', 'reason', 'dynamic_steps'}."""
    def bad(reason):
        return {"ok": False, "reason": reason, "dynamic_steps": 0}

    if not words:
        return bad("program contains no instructions")
    words = [_u32(w) for w in words]
    if base != 0:
        return bad("base must be 0 (PROGADDR_RESET of the DUT)")
    for index, word in enumerate(words):
        reason = _static_check(index, word)
        if reason:
            return bad(reason)

    ref = RV32IReference(words, base)
    end = base + 4 * len(words)
    max_steps = 8 * len(words) + 64
    steps = 0
    try:
        while base <= ref.pc < end and steps < max_steps:
            if ref.pc & 3:
                return bad(f"instruction {steps}: misaligned fetch at 0x{ref.pc:08x}")
            event = ref.step()
            insn = event["insn"]
            opcode, funct3 = insn & 0x7F, (insn >> 12) & 7
            if opcode in (0x03, 0x23):
                size = {0: 1, 1: 2, 2: 4, 4: 1, 5: 2}[funct3] if opcode == 0x03 else {0: 1, 1: 2, 2: 4}[funct3]
                addr = event["mem_addr"]
                if addr % size:
                    return bad(f"instruction {steps}: unaligned {size}-byte memory access at 0x{addr:08x}")
                if addr + size > HW_MEM_SIZE:
                    return bad(f"instruction {steps}: access 0x{addr:08x} outside the {HW_MEM_SIZE // 1024} KiB memory")
                if addr < end and addr + size > base:
                    return bad(f"instruction {steps}: access 0x{addr:08x} overlaps the program image")
            steps += 1
    except ValueError as exc:
        return bad(str(exc))
    if base <= ref.pc < end:
        return bad("program does not terminate within the step bound")
    if ref.pc != end:
        return bad(f"control flow leaves the program image (final pc 0x{ref.pc:08x}, expected 0x{end:08x})")
    return {"ok": True, "reason": "", "dynamic_steps": steps}


def validate_instruction_sequence(words: list[int], base: int = 0) -> tuple[bool, str]:
    res = analyze_program(words, base)
    return res["ok"], res["reason"]


def validate_program(program: dict) -> dict:
    """Structured validation result suitable for campaign telemetry and UI."""
    steps = 0
    try:
        words = [_as_int(word) for word in program.get("words", [])]
        res = analyze_program(words, _as_int(program.get("base", 0)))
        valid, reason, steps = res["ok"], res["reason"], res["dynamic_steps"]
    except (TypeError, ValueError) as exc:
        valid, reason = False, f"invalid instruction encoding: {exc}"
    return {
        "program_id": program.get("id", "unnamed"),
        "valid": valid,
        "reason": reason,
        "instruction_count": len(program.get("words", [])),
        "dynamic_steps": steps,
    }


# ── Temp dirs, lint, yosys ───────────────────────────────────────────────────

def get_space_safe_tempdir(prefix: str = "benchfuzz-picorv32-") -> Path:
    base_tmp = "/tmp" if os.path.exists("/tmp") and os.access("/tmp", os.W_OK) else tempfile.gettempdir()
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base_tmp))


def run_verilator_lint(rtl_content: str | None = None, rtl_path: Path | None = None,
                       workdir: Path | None = None) -> tuple[bool, str]:
    """Verilator lint. Uses its own sub-directory so it never clobbers the simulator sources."""
    cleanup = workdir is None
    root = get_space_safe_tempdir("benchfuzz-lint-") if workdir is None else Path(workdir)
    lint_dir = root / "lint"
    try:
        lint_dir.mkdir(parents=True, exist_ok=True)
        target_v = lint_dir / "picorv32.v"
        if rtl_content is not None:
            target_v.write_text(rtl_content)
        elif rtl_path is not None:
            shutil.copyfile(rtl_path, target_v)
        else:
            return False, "No RTL content or path provided."
        cmd = ["verilator", "--lint-only", *_VERILATOR_WARN_FLAGS, "-DRISCV_FORMAL",
               "--top-module", "picorv32", *_param_flags(), "picorv32.v"]
        res = subprocess.run(cmd, cwd=lint_dir, capture_output=True, text=True)
        log = (res.stdout + "\n" + res.stderr).strip()
        return (res.returncode == 0 and "%Error" not in log), log
    except FileNotFoundError:
        return False, "verilator executable not found in PATH."
    except Exception as exc:
        return False, f"Verilator lint execution error: {exc}"
    finally:
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)


def estimate_yosys_stats(rtl_path: Path, workdir: Path | None = None) -> dict:
    """Flattened Yosys cell/wire count of the configured core (JSON written with `tee -o`)."""
    cleanup = workdir is None
    root = get_space_safe_tempdir("benchfuzz-yosys-") if workdir is None else Path(workdir)
    ys_dir = root / "yosys"
    stats = {"cells": {}, "total_cells": 0, "wires": 0, "memories": 0, "log": ""}
    try:
        ys_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rtl_path, ys_dir / "picorv32.v")
        sets = " ".join(f"-set {k} {v}" for k, v in HW_PARAMS.items())
        script = (f"read_verilog -sv picorv32.v\nchparam {sets} picorv32\n"
                  "hierarchy -check -top picorv32\nproc\nflatten\nopt\n"
                  "tee -o stats.json stat -json\n")
        (ys_dir / "synth_stats.ys").write_text(script)
        res = subprocess.run(["yosys", "-q", "-s", "synth_stats.ys"], cwd=ys_dir, capture_output=True, text=True)
        stats["log"] = (res.stdout + "\n" + res.stderr).strip()[-3000:]
        stats_file = ys_dir / "stats.json"
        if res.returncode == 0 and stats_file.exists():
            data = json.loads(stats_file.read_text())
            design = data.get("design") or {}
            mods = data.get("modules") or {}
            top = next(iter(mods.values()), {}) if mods else {}
            src = design if "num_cells" in design else top
            stats["total_cells"] = int(src.get("num_cells", 0))
            stats["wires"] = int(src.get("num_wires", 0))
            cells = src.get("num_cells_by_type") or src.get("cells") or {}
            stats["cells"] = {k: int(v) for k, v in cells.items() if isinstance(v, (int, float))}
            stats["memories"] = int(src.get("num_memories", 0)) or sum(
                c for n, c in stats["cells"].items() if "MEM" in n.upper())
    except Exception as exc:
        stats["log"] = f"Yosys estimation unavailable: {exc}"
    finally:
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)
    return stats


# ── Verilator coverage (.dat parsing and cumulative merge) ───────────────────

_DAT_LINE = re.compile(r"^C '(.*)' (\d+)\s*$")


def parse_verilator_dat(path: str | Path) -> dict[str, int]:
    """Parse a Verilator coverage .dat file into {point-key: hit-count}."""
    points: dict[str, int] = {}
    for raw in Path(path).read_text(errors="ignore").splitlines():
        m = _DAT_LINE.match(raw)
        if m:
            points[m.group(1)] = points.get(m.group(1), 0) + int(m.group(2))
    return points


def _page_of(key: str) -> str:
    m = re.search(r"(?:\x01|\\001)page(?:\x02|\\002)([^\x01\\]+)", key)
    return m.group(1).split("/")[0] if m else "unknown"


def coverage_percentages(points: dict[str, int]) -> dict:
    """Percentage of points with a non-zero count, overall and per coverage kind."""
    by_page: dict[str, list[int]] = {}
    for key, cnt in points.items():
        by_page.setdefault(_page_of(key), []).append(cnt)

    def pct(counts):
        return round(100.0 * sum(c > 0 for c in counts) / len(counts), 2) if counts else None

    allc = [c for cs in by_page.values() for c in cs]
    return {"line": pct(by_page.get("v_line", [])), "toggle": pct(by_page.get("v_toggle", [])),
            "total": pct(allc), "points": len(allc)}


def summarize_verilator_coverage(coverage_paths: list[str | Path], cumulative: dict[str, int] | None = None) -> dict:
    """Merge this iteration's coverage into ``cumulative`` (in place) and report percentages.

    ``cumulative`` makes the reported percentages a true union over the campaign;
    without it the result describes only the supplied files.
    """
    paths = [Path(p) for p in coverage_paths if p and Path(p).exists()]
    summary = {"artifact_count": len(paths), "available": bool(paths),
               "tool_available": shutil.which("verilator_coverage") is not None,
               "line_coverage_pct": None, "toggle_coverage_pct": None, "total_coverage_pct": None,
               "annotated_path": None, "cumulative": cumulative is not None}
    if not paths:
        return summary
    merged = cumulative if cumulative is not None else {}
    for p in paths:
        try:
            for key, cnt in parse_verilator_dat(p).items():
                merged[key] = merged.get(key, 0) + cnt
        except OSError:
            continue
    pcts = coverage_percentages(merged)
    summary.update(line_coverage_pct=pcts["line"], toggle_coverage_pct=pcts["toggle"],
                   total_coverage_pct=pcts["total"], coverage_points=pcts["points"])
    return summary


# ── C++ harness ──────────────────────────────────────────────────────────────

CPP_HARNESS_TEMPLATE = """#include <algorithm>
#include <iostream>
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
    int expected_arg = (argc > 6) ? std::atoi(argv[6]) : 0;   // dynamic instruction count from the ISS

    std::ofstream trace_file;
    if (!jsonl_path.empty()) trace_file.open(jsonl_path);

    std::memset(memory, 0, sizeof(memory));
    std::srand(12345);

    int static_words = 0;
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
            static_words++;
        }
    }
    int expected_total_retires = expected_arg > 0 ? expected_arg : static_words;

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

    uint64_t main_time = 0;
    int total_cycles = 0;
    int retired = 0;
    int cycles_since_retire = 0;
    int mem_delay_counter = 0;
    const int max_cycles_without_retire = 200;
    const int max_total_cycles = std::max(500, expected_total_retires * 160);

    for (int cycle = 0; cycle < max_total_cycles; ++cycle) {
        if (cycle >= 10) top->resetn = 1;

        top->clk = 0;
        top->eval();
#if VM_TRACE
        if (tfp) tfp->dump(main_time);
#endif
        main_time += 5;

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
                if (mem_delay_max > 0) mem_delay_counter = (std::rand() % (mem_delay_max + 1));
            }
        } else {
            top->mem_ready = 0;
        }

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
    if (!coverage_path.empty()) VerilatedCov::write(coverage_path.c_str());
#endif
    if (trace_file.is_open()) trace_file.close();
    delete top;
    return 0;
}
"""


def _build_stamp(rtl_text: str) -> str:
    h = hashlib.sha256()
    h.update(rtl_text.encode())
    h.update(CPP_HARNESS_TEMPLATE.encode())
    h.update(" ".join(_param_flags()).encode())
    return h.hexdigest()


def _ensure_sim_binary(rtl_path: Path, workdir: Path) -> tuple[Path | None, str]:
    """Compile the reusable simulator if the RTL/harness/parameters changed (content hash)."""
    workdir.mkdir(parents=True, exist_ok=True)
    rtl_text = Path(rtl_path).read_text()
    stamp = _build_stamp(rtl_text)
    stamp_file = workdir / ".build_stamp"
    sim_binary = workdir / "obj_dir" / "sim_picorv32"

    if sim_binary.exists() and stamp_file.exists() and stamp_file.read_text() == stamp:
        return sim_binary, ""

    stamp_file.unlink(missing_ok=True)
    if (workdir / "obj_dir").exists():
        shutil.rmtree(workdir / "obj_dir", ignore_errors=True)
    (workdir / "picorv32.v").write_text(rtl_text)
    (workdir / "picorv32_harness.cpp").write_text(CPP_HARNESS_TEMPLATE)

    cmd = ["verilator", *_VERILATOR_WARN_FLAGS, "-DRISCV_FORMAL", "--top-module", "picorv32",
           *_param_flags(), "--cc", "picorv32.v", "--exe", "picorv32_harness.cpp",
           "--trace", "--coverage", "--coverage-line", "--coverage-toggle", "-o", "sim_picorv32"]
    try:
        res = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    except FileNotFoundError:
        return None, "verilator executable not found in PATH."
    lint_log = (res.stdout + "\n" + res.stderr).strip()
    if res.returncode != 0 or "%Error" in lint_log:
        return None, lint_log
    make = subprocess.run(["make", "-C", "obj_dir", "-f", "Vpicorv32.mk", "-j4"],
                          cwd=workdir, capture_output=True, text=True)
    if make.returncode != 0:
        return None, (make.stdout + "\n" + make.stderr).strip()
    if not sim_binary.exists():
        return None, "simulator binary missing after make"
    stamp_file.write_text(stamp)
    return sim_binary, lint_log


def prepare_simulator(rtl_path: Path, workdir: Path) -> tuple[bool, str]:
    """Build (or reuse) the simulator ahead of time, e.g. so timing excludes compilation."""
    binary, log = _ensure_sim_binary(rtl_path, workdir)
    return binary is not None, log


def _failed(error: str, lint_log: str, validation: dict) -> dict:
    return {"ok": False, "error": error, "lint_log": lint_log, "hardware_trace": [], "hardware_trap": False,
            "retired": 0, "cycles": 0, "cpi": 0.0, "vcd_path": None, "coverage_path": None,
            "validation": validation}


def build_and_simulate_program(
    rtl_path: Path,
    program: dict,
    workdir: Path,
    iteration: int = 0,
    generate_vcd: bool = True,
    mem_delay: int = 0,
    vcd_name: str | None = None,
) -> dict:
    """Simulate a program on the Verilated RTL and return the RVFI retirement trace."""
    validation = validate_program(program)
    if not validation["valid"]:
        return _failed(f"Instruction validation failed: {validation['reason']}", "", validation)

    sim_binary, lint_log = _ensure_sim_binary(rtl_path, workdir)
    if sim_binary is None:
        return _failed("Simulator binary compilation failed", lint_log, validation)

    words = [_as_int(w) for w in program.get("words", [])]
    prog_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(program.get("id", f"iter{iteration:04d}")))
    hex_path = workdir / f"{prog_id}.hex"
    hex_path.write_text("\n".join(f"{w & 0xFFFFFFFF:08x}" for w in words) + "\n")

    vcd_path = (workdir / (vcd_name or f"picorv32_iter{iteration:04d}.vcd")) if generate_vcd else None
    jsonl_path = workdir / f"{prog_id}.jsonl"
    coverage_path = workdir / f"{prog_id}.coverage.dat"
    coverage_path.unlink(missing_ok=True)

    exec_args = [str(sim_binary), str(hex_path), str(vcd_path) if vcd_path else "", str(jsonl_path),
                 str(mem_delay), str(coverage_path), str(validation["dynamic_steps"])]
    try:
        sim_res = subprocess.run(exec_args, cwd=workdir, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return _failed("simulation timed out after 30 seconds", lint_log, validation)
    if sim_res.returncode != 0:
        return _failed(f"simulator exited with code {sim_res.returncode}: {sim_res.stderr[-500:]}",
                       lint_log, validation)

    hardware_trace, hardware_trap = [], False
    for line in sim_res.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("trap"):
                hardware_trap = True
            elif rec.get("valid"):
                hardware_trace.append(rec)

    total_cycles = hardware_trace[-1].get("cycles", 0) if hardware_trace else 0
    retired_count = len(hardware_trace)
    return {
        "ok": True, "lint_log": lint_log, "hardware_trace": hardware_trace, "hardware_trap": hardware_trap,
        "retired": retired_count, "cycles": total_cycles, "cpi": round(total_cycles / max(1, retired_count), 2),
        "vcd_path": str(vcd_path) if (vcd_path and vcd_path.exists()) else None,
        "coverage_path": str(coverage_path) if coverage_path.exists() else None,
        "validation": validation, "mem_delay": mem_delay,
    }


# ── RVFI lockstep scoreboard ─────────────────────────────────────────────────

def _lane_mask(mask: int) -> int:
    return sum(0xFF << (8 * i) for i in range(4) if mask & (1 << i))


def compare_rvfi_trace(program: dict, hardware_trace: list[dict], normalize_pc: bool = False,
                       hardware_trap: bool = False, aligned_mem: bool | None = None) -> dict:
    """Compare the DUT retirement trace against the ISS in lockstep.

    Beyond per-field comparison this reports
    * ``missing_retire``: the DUT stopped (typically trapped) while the ISS
      still had instructions to retire, and
    * ``extra_retire``: the DUT retired instructions after the ISS finished.
    The prototype silently treated both as a pass.
    """
    words = [_as_int(w) for w in program.get("words", [])]
    base = _as_int(program.get("base", 0))
    prog_end = base + len(words) * 4
    reference = RV32IReference(words, base)
    cov_tracker = CoverageTracker()
    mismatches, matches = [], []
    hardware_pc_base = None

    def disasm_of(insn):
        return disassemble_rv32i(insn)

    for order, actual in enumerate(hardware_trace):
        if not actual.get("valid", False):
            continue
        if hardware_pc_base is None:
            hardware_pc_base = _as_int(actual.get("pc_rdata", 0))

        if reference.pc >= prog_end or reference.pc < base:
            insn_val = _as_int(actual.get("insn", 0))
            mismatches.append({
                "retire_order": order, "kind": "extra_retire", "pc": actual.get("pc_rdata", 0),
                "insn_hex": f"0x{insn_val:08x}", "insn_disasm": disasm_of(insn_val),
                "expected": None, "actual": actual,
                "error": "hardware retired an instruction after the reference model finished"})
            break

        try:
            expected = reference.step()
        except ValueError as exc:
            insn_val = _as_int(actual.get("insn", 0))
            mismatches.append({
                "retire_order": order, "kind": "reference_error", "pc": actual.get("pc_rdata", 0),
                "insn_hex": f"0x{insn_val:08x}", "insn_disasm": disasm_of(insn_val),
                "expected": None, "actual": actual, "error": str(exc)})
            break

        insn_val = _as_int(actual.get("insn", expected.get("insn", 0)))
        cov_tracker.sample_instruction(insn_val, expected.get("rd_wdata", 0), wrote_rd=expected["rd_addr"] != 0)

        diffs = {}
        for field in ("pc_rdata", "pc_wdata", "insn", "rd_addr", "rd_wdata", "mem_addr", "mem_rmask", "mem_wmask"):
            hw_val = _as_int(actual.get(field, -1))
            sw_val = expected[field]
            if field == "mem_addr" and (RVFI_ALIGNED_MEM if aligned_mem is None else aligned_mem) and (expected["mem_rmask"] or expected["mem_wmask"]):
                sw_val &= ~3          # DUT convention: word address + byte-lane mask
            if normalize_pc and field in {"pc_rdata", "pc_wdata"}:
                hw_val = _u32(hw_val - hardware_pc_base)
                sw_val = _u32(sw_val - base)
            if hw_val != sw_val:
                diffs[field] = {"expected": sw_val, "actual": hw_val}

        wmask = expected.get("mem_wmask", 0) & 0xF
        if wmask:
            lanes = _lane_mask(wmask)
            hw_w, sw_w = _as_int(actual.get("mem_wdata", -1)), expected.get("mem_wdata", 0)
            if (hw_w & lanes) != (sw_w & lanes):
                diffs["mem_wdata"] = {"expected": sw_w & lanes, "actual": hw_w & lanes}
        rmask = expected.get("mem_rmask", 0) & 0xF
        if rmask:
            lanes = _lane_mask(rmask)
            hw_r, sw_r = _as_int(actual.get("mem_rdata", -1)), expected.get("mem_rdata", 0)
            if (hw_r & lanes) != (sw_r & lanes):
                diffs["mem_rdata"] = {"expected": sw_r & lanes, "actual": hw_r & lanes}

        record = {"retire_order": order, "pc": actual.get("pc_rdata", 0), "insn_hex": f"0x{insn_val:08x}",
                  "insn_disasm": disasm_of(insn_val), "expected": expected, "actual": actual}
        if diffs:
            record["differences"] = diffs
            record["kind"] = "field_mismatch"
            mismatches.append(record)
            break
        matches.append(record)

    if not mismatches and base <= reference.pc < prog_end:
        probe = reference.clone()
        try:
            expected = probe.step()
            insn_val = expected["insn"]
        except ValueError:
            expected, insn_val = None, reference.peek_insn()
        mismatches.append({
            "retire_order": len(matches), "kind": "missing_retire", "pc": reference.pc,
            "insn_hex": f"0x{insn_val:08x}", "insn_disasm": disasm_of(insn_val),
            "expected": expected, "actual": None,
            "error": ("hardware trapped before retiring this instruction" if hardware_trap
                      else "hardware stopped retiring before the reference model finished")})

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
