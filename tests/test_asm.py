import random, unittest
from harness.asm import assemble, AsmError
from harness.delta_debugger import disassemble_rv32i
from harness.riscv_cosim import analyze_program
from sw_reference.golden_rv32i import RV32IReference, u32
from tests.mock_dut import addi, xori, rtype, sw, lw, lb, beq


class TestAsm(unittest.TestCase):
    def test_matches_independent_encoders(self):
        self.assertEqual(assemble("addi x1, x0, 5\nxori x2, x1, -1\nadd x3, x1, x2\nmul x4, x1, x2\nsw x3, 8(x1)\nlw x5, 8(x1)\nlb x6, 3(x1)\nbeq x1, x2, 8")[:7],
                         [addi(1, 0, 5), xori(2, 1, -1), rtype(0, 2, 1, 0, 3), rtype(1, 2, 1, 0, 4), sw(3, 1, 8), lw(5, 1, 8), lb(6, 1, 3)])
        self.assertEqual(assemble("beq x1, x2, 8")[0], beq(1, 2, 8))

    def test_roundtrip_disassembly(self):
        src = ["add x3, x1, x2", "sub x3, x1, x2", "slli x4, x1, 7", "srai x4, x1, 3", "lui x5, 0x12345", "auipc x6, 0x1",
               "sltu x7, x1, x2", "sb x2, 5(x1)", "lhu x9, 2(x1)", "div x1, x2, x3", "remu x1, x2, x3"]
        for line in src:
            w = assemble(line)[0]
            d = disassemble_rv32i(w).replace(" ", "").lower(); self.assertEqual(d.replace("0x12345", "0x12345"), line.replace(" ", "").lower().replace("0x12345", "0x12345"), (line, d))

    def test_pseudo_li_and_labels_execute_correctly(self):
        w = assemble("""
            li   t0, 0x12345678      # two instructions
            li   t1, -1
            li   t2, 0x800           # needs lui+addi
            mv   a0, t0
            beqz zero, skip
            li   a1, 99
        skip:
            not  a2, t1
            nop
        """)
        r = RV32IReference(w)
        while r.pc < 4 * len(w): r.step()
        self.assertEqual((r.regs[5], r.regs[6], r.regs[7], r.regs[10], r.regs[11], r.regs[12]), (0x12345678, 0xFFFFFFFF, 0x800, 0x12345678, 0, 0))
        self.assertTrue(analyze_program(w)["ok"])

    def test_li_all_ranges(self):
        rng = random.Random(3)
        for v in [0, 1, -1, 2047, 2048, -2048, -2049, 0x7FFFFFFF, 0x80000000, 0xFFFFF800, 0x12345FFF] + [rng.getrandbits(32) for _ in range(300)]:
            w = assemble(f"li x5, {v}")
            r = RV32IReference(w)
            for _ in w: r.step()
            self.assertEqual(r.regs[5], u32(v), hex(v))

    def test_errors_are_line_numbered_for_llm_feedback(self):
        for bad, frag in [("addi x1, x0, 5000", "out of range"), ("foo x1, x2", "unknown"), ("add x1, x2", "needs 3"),
                          ("beq x1, x2, nowhere", "unresolved label"), ("lw x1, x2", "imm(reg)"), ("addi x99, x0, 1", "invalid register"),
                          ("a:\na:\nnop", "duplicate")]:
            with self.assertRaises(AsmError) as cm: assemble(bad)
            self.assertIn(frag, str(cm.exception)); self.assertRegex(str(cm.exception), r"line \d+")


if __name__ == "__main__":
    unittest.main()
