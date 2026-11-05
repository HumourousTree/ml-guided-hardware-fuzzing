"""Rocket generator (rocket/rv64fuzz.py): program text invariants that keep the Spike comparison free of false alarms.
No RISC-V toolchain needed (the calibration against Spike is done by rocket/run_encarsia.py)."""
import random
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rocket"))
import rv64fuzz  # noqa: E402


def body(asm: str) -> list[str]:
    lines = [l.strip() for l in asm.splitlines()]
    start = next(i for i, l in enumerate(lines) if l.startswith("fsrmi x0")) + 1   # last prologue line
    end = next(i for i, l in enumerate(lines) if i >= start and l.startswith("li x31,"))   # first epilogue line
    return lines[start:end]


def dest(line: str) -> str | None:
    m = re.match(r"([a-z.]+)\s+(x\d+),", line)
    if not m or m.group(1).startswith(("s", "b", "c.b", "c.s")) and m.group(1) not in (
            "sub", "subw", "sll", "slt", "sltu", "sra", "srl", "slli", "srli", "srai", "slliw", "srliw", "sraiw",
            "sllw", "srlw", "sraw", "slti", "sltiu", "c.sub", "c.subw", "c.slli", "c.srli", "c.srai"):
        return None
    return m.group(2)


class TestRv64Fuzz(unittest.TestCase):
    def test_deterministic(self):
        for p in rv64fuzz.PROFILES:
            self.assertEqual(rv64fuzz.gen_program(random.Random(3), 40, True, p),
                             rv64fuzz.gen_program(random.Random(3), 40, True, p))

    def test_base_profile_has_no_traps_csrs_or_rvc(self):
        for s in range(30):
            asm = rv64fuzz.gen_program(random.Random(s), 40, True, "base")
            self.assertNotRegex(asm, r"\b(ecall|ebreak|mtvec|mepc|mcause|mtval|\.word|c\.\w+)\b")
            self.assertIn(".option norvc", asm)
            self.assertNotIn(".option rvc", asm)

    def test_reserved_registers_never_written_in_body(self):
        for p, reserved in (("base", {"x30", "x31"}), ("ext", {"x28", "x29", "x30", "x31"})):
            for s in range(40):
                for line in body(rv64fuzz.gen_program(random.Random(s), 40, True, p)):
                    if p == "ext" and line.startswith("li x28,"):      # mstatus mask / bit temporaries
                        continue
                    self.assertNotIn(dest(line), reserved, f"{p} seed {s}: {line}")

    def test_ext_trapping_instructions_are_4_bytes(self):
        """the handler skips 4 bytes, so every intentionally trapping instruction is assembled with norvc"""
        for s in range(40):
            lines = body(rv64fuzz.gen_program(random.Random(s), 40, True, "ext"))
            for i, l in enumerate(lines):
                if l in ("ecall", "ebreak") or l.startswith(".word"):
                    self.assertEqual(lines[i - 1], ".option norvc", l)

    def test_ext_never_sets_mie_or_leaves_m_mode(self):
        for s in range(40):
            asm = rv64fuzz.gen_program(random.Random(s), 60, True, "ext")
            for v in re.findall(r"li x28, (\d+)\n\s*csr[sc] mstatus", asm):
                self.assertEqual(int(v) & ~(0x6000 | 0x80), 0, v)      # FS and MPIE only
            self.assertNotRegex(asm, r"csr\w* \S+, mtvec, |csrw mtvec, x(?!28)")

    def test_ext_masks_fs_in_mstatus_reads(self):
        for s in range(40):
            lines = body(rv64fuzz.gen_program(random.Random(s), 60, True, "ext"))
            for i, l in enumerate(lines):
                if re.match(r"csrr x\d+, mstatus", l):
                    self.assertEqual(lines[i + 1], "li x28, 0x7FFFFFFFFFFF9FFF")


if __name__ == "__main__":
    unittest.main()
