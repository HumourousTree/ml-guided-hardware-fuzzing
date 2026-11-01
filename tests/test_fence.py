"""FENCE support in the reference, the validator and the assembler (Cascade P6 / reserved-field study)."""
import unittest

from harness.asm import assemble
from harness.riscv_cosim import analyze_program
from sw_reference.golden_rv32i import RV32IReference
from tests.mock_dut import addi, NOP

FENCE, FENCE_RD1, FENCE_I = 0x0FF0000F, 0x0FF0008F, 0x0000100F


class TestFence(unittest.TestCase):
    def test_reference_treats_fence_as_nop_and_ignores_rd(self):
        iss = RV32IReference([addi(1, 0, 123), FENCE_RD1, addi(2, 1, 0), NOP])
        recs = [iss.step() for _ in range(3)]
        self.assertEqual((recs[1]["rd_addr"], recs[1]["rd_wdata"]), (0, 0))
        self.assertEqual(recs[1]["pc_wdata"], 8)
        self.assertEqual(iss.regs[2], 123)            # rd field of the fence did not clobber x1

    def test_validator_accepts_fence_rejects_fence_i(self):
        self.assertTrue(analyze_program([FENCE, NOP])["ok"])
        r = analyze_program([FENCE_I, NOP])
        self.assertFalse(r["ok"]); self.assertIn("FENCE.I", r["reason"])

    def test_assembler(self):
        self.assertEqual(assemble("fence\nfence iorw, iorw\nnop"), [FENCE, FENCE, NOP])


if __name__ == "__main__":
    unittest.main()
