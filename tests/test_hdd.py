"""Hierarchical delta debugging and 1-minimality (pure Python, no Verilator)."""
import unittest

from harness.delta_debugger import basic_blocks, ddmin_reproducer, hdd_reproducer, is_one_minimal
from tests.mock_dut import addi, beq, NOP


def prog(words):
    return {"id": "p", "base": "0x00000000", "words": [f"0x{w:08x}" for w in words]}


class TestHDD(unittest.TestCase):
    def test_basic_blocks_split_after_branch_and_at_target(self):
        w = [addi(1, 0, 1), beq(1, 2, 8), addi(3, 0, 3), addi(4, 0, 4), addi(5, 0, 5)]
        # beq at index 1 jumps to index 3 -> leaders 0, 2, 3
        self.assertEqual([len(b) for b in basic_blocks(w)], [2, 1, 2])

    def test_hdd_and_ddmin_find_the_same_culprits(self):
        culprit_a, culprit_b = addi(7, 0, 7), addi(9, 0, 9)
        noise = [addi(r, 0, r) for r in range(10, 30)]
        words = noise[:5] + [culprit_a] + noise[5:12] + [culprit_b] + noise[12:] + [addi(1, 1, 1), NOP]
        fails = lambda p: {f"0x{culprit_a:08x}", f"0x{culprit_b:08x}"} <= set(p["words"])
        flat, log = ddmin_reproducer(prog(words), fails)
        hier, stats = hdd_reproducer(prog(words), fails)
        self.assertEqual(len(flat["words"]), 4)
        self.assertEqual(hier["words"], flat["words"])
        self.assertTrue(is_one_minimal(hier, fails)[0])
        self.assertEqual(stats["tests"], sum(l["tests"] for l in stats["levels"]))

    def test_hdd_drops_whole_blocks_first(self):
        # culprit sits in the last block; every earlier block can go in one coarse step
        body = [addi(1, 0, 1), beq(0, 0, 8), addi(2, 0, 2), addi(3, 0, 3), beq(0, 0, 8), addi(4, 0, 4), addi(8, 0, 8)]
        culprit = f"0x{addi(8, 0, 8):08x}"
        fails = lambda p: culprit in p["words"]
        hier, stats = hdd_reproducer(prog(body + [addi(1, 1, 1), NOP]), fails)
        self.assertIn(culprit, hier["words"])
        self.assertLess(stats["levels"][0]["units_out"], stats["levels"][0]["units_in"])

    def test_not_one_minimal_detected(self):
        a = addi(7, 0, 7)
        fails = lambda p: f"0x{a:08x}" in p["words"]
        self.assertFalse(is_one_minimal(prog([addi(1, 0, 1), a, addi(1, 1, 1), NOP]), fails)[0])


if __name__ == "__main__":
    unittest.main()
