"""Template-repair operators and oracle hunks (pure functions; no Verilator)."""
import unittest
from pathlib import Path

from harness.template_repair import golden_hunks, template_variants, _offered_blocks, _offered_lines

HW = Path(__file__).resolve().parents[1] / "hw_design"


def norm(s):
    return " ".join(s.split())


class TestTemplates(unittest.TestCase):
    def has(self, line, fixed):
        self.assertIn(norm(fixed), [norm(v) for v in template_variants(line)], line)

    def test_operator_swap(self):
        self.has("\t\talu_out = reg_op1 | reg_op2;", "alu_out = reg_op1 & reg_op2;")

    def test_negation_removal(self):
        self.has("\t\t\talu_out_0 = !alu_eq;", "alu_out_0 = alu_eq;")

    def test_ternary_collapse(self):
        self.has("latched_rd <= (instr_lui || instr_auipc) ? 6'd0 : decoded_rd;", "latched_rd <= decoded_rd;")
        self.has("alu_out = reg_op2[31] ? (reg_op1 | reg_op2) : (reg_op1 ^ reg_op2);", "alu_out = (reg_op1 ^ reg_op2);")

    def test_operand_and_element_drop(self):
        self.has("mem_wstrb <= mem_la_wstrb & {4{mem_la_write && !bug_prev_data_store}};",
                 "mem_wstrb <= mem_la_wstrb & {4{mem_la_write}};")
        self.has("wire instr_rs2_signed = |{instr_mulh, instr_mulhsu};", "wire instr_rs2_signed = |{instr_mulh};")
        self.has("assign mem_la_write = resetn && !mem_state && mem_do_wdata && !(reg_op1[1:0] == 2'b10 && mem_wordsize == 1);",
                 "assign mem_la_write = resetn && !mem_state && mem_do_wdata;")

    def test_signed_wrap(self):
        self.has("alu_lts = reg_op1 < reg_op2;", "alu_lts = $signed(reg_op1) < $signed(reg_op2);")

    def test_bit_slices_are_not_ternaries(self):
        for v in template_variants("x = a[3:0] & b;"):
            self.assertNotIn("[3", v.replace("a[3:0]", "")) if "a[3:0]" not in v else None

    def test_no_rhs_no_variants(self):
        self.assertEqual(template_variants("\t\tend"), [])

    def test_golden_hunks_insertion_and_replacement(self):
        g = "a\nb\nc\nd\n"
        self.assertEqual(golden_hunks("a\nB\nc\nd\n", g), [(2, 2, "b")])
        self.assertEqual(golden_hunks("a\nb\nd\n", g), [(2, 2, "b\nc")])          # deleted line re-inserted
        self.assertEqual(golden_hunks("a\nb\nX\nc\nd\n", g), [(2, 3, "b")])       # extra line removed

    def test_prompt_parsing(self):
        p = "--- Candidate block 1: ALU (lines 10-12) ---\n```verilog\n  10: \tx = a;\n  11: \ty = b;\n```"
        self.assertEqual(_offered_blocks(p), [(10, 12)])
        self.assertEqual(_offered_lines(p), {10: "\tx = a;", 11: "\ty = b;"})


if __name__ == "__main__":
    unittest.main()
