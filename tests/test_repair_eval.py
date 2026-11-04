"""Blind-mode prompt of the LLM repair evaluation (reading-vs-reasoning control)."""
import unittest

from harness.delta_debugger import format_localized_repair_prompt
from harness.repair_eval import blind_prompt, pass_at_k


class TestBlindPrompt(unittest.TestCase):
    def setUp(self):
        self.args = ({"id": "repro-7", "words": ["0x00100093", "0x00000013"]},
                     {"insn_disasm": "add x3, x1, x2", "insn_hex": "0x002081b3", "retire_order": 4,
                      "differences": {"rd_wdata": {"expected": 5, "actual": 7}}},
                     {"candidates": [{"subsystem": "alu", "start_line": 10, "end_line": 12,
                                      "diagnosis": "rd_wdata differs on an ALU instruction",
                                      "numbered_snippet": "  10: \talu_out = reg_op1 | reg_op2;"}]})

    def test_informed_prompt_contains_failure_data(self):
        p = format_localized_repair_prompt(*self.args)
        self.assertIn("rd_wdata", p); self.assertIn("add x3, x1, x2", p)

    def test_blind_prompt_keeps_blocks_and_task_but_no_failure_data(self):
        p = blind_prompt(format_localized_repair_prompt)(*self.args)
        for leaked in ("rd_wdata", "add x3, x1, x2", "repro-7", "expected", "0x002081b3", "ALU instruction"):
            self.assertNotIn(leaked, p)
        self.assertIn("alu_out = reg_op1 | reg_op2", p)           # the code is still offered
        self.assertIn("// REPLACE LINES", p)                        # same answer format
        self.assertIn("injected defect", p)

    def test_pass_at_k(self):
        self.assertEqual(pass_at_k(5, 0, 1), 0.0); self.assertEqual(pass_at_k(5, 5, 1), 1.0)
        self.assertAlmostEqual(pass_at_k(5, 1, 5), 1.0); self.assertAlmostEqual(pass_at_k(5, 1, 1), 0.2)


if __name__ == "__main__":
    unittest.main()
