"""End-state oracle versus lockstep oracle on traces built from the reference model."""
import unittest

from harness.riscv_cosim import compare_end_state, compare_rvfi_trace
from sw_reference.golden_rv32i import RV32IReference
from tests.mock_dut import addi, NOP


def sw(rs2, rs1, imm): return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((imm & 31) << 7) | 0x23


def trace_of(words):
    """Hardware-like trace: reference records with valid flags and the word-aligned mem_addr convention."""
    iss, out = RV32IReference(words), []
    for _ in range(len(words)):
        r = dict(iss.step(), valid=True)
        if r["mem_rmask"] or r["mem_wmask"]:
            r["mem_addr"] &= ~3
        out.append(r)
    return out


class TestEndState(unittest.TestCase):
    words = [addi(1, 0, 0x100), addi(2, 0, 5), addi(2, 2, 1), sw(2, 1, 0), addi(3, 0, 9), NOP]
    prog = {"id": "p", "base": "0x0", "words": [f"0x{w:08x}" for w in words]}

    def test_correct_trace_passes_both_oracles(self):
        t = trace_of(self.words)
        self.assertTrue(compare_end_state(self.prog, t)["ok"])
        self.assertTrue(compare_rvfi_trace(self.prog, t)["ok"])

    def test_overwritten_intermediate_value_is_invisible_to_end_state(self):
        t = trace_of(self.words)
        t[1]["rd_wdata"] = 77            # x2 = 77 instead of 5 ... but the next instruction recomputes x2 from the true value
        self.assertTrue(compare_end_state(self.prog, t)["ok"])
        self.assertFalse(compare_rvfi_trace(self.prog, t)["ok"])

    def test_wrong_final_register_and_store_are_visible_to_both(self):
        t = trace_of(self.words)
        t[4]["rd_wdata"] = 8
        self.assertEqual(list(compare_end_state(self.prog, t)["reg_diffs"]), ["x3"])
        t = trace_of(self.words)
        t[3]["mem_wdata"] = 0x99
        self.assertIn("0x00000100", compare_end_state(self.prog, t)["mem_diffs"])


if __name__ == "__main__":
    unittest.main()
