"""Checks against the real RTL/manifest in the repository (skipped when the files are absent)."""
import json, unittest
from pathlib import Path
from harness.delta_debugger import localize_rtl_fault
from tests.mock_dut import xori, addi, rtype, sw, lw, beq, NOP

HW = Path(__file__).resolve().parents[1] / "hw_design"
need = unittest.skipUnless((HW / "picorv32_golden.sv").exists() and (HW / "bug_manifest.json").exists(), "RTL not present")

# an RVFI failing-instruction signature for each bug in bug_manifest.json
SIGNATURE = {
    "approx_multiplier": ({"insn": rtype(1, 2, 1, 0, 3)}, {"rd_wdata": {}}),
    "alu_operator_xor_or": ({"insn": xori(2, 1, -1)}, {"rd_wdata": {}}),
    "branch_inversion_blt": ({"insn": beq(1, 2, 8) | 0x4000}, {"pc_wdata": {}}),
    "writeback_forwarding_drop": ({"insn": addi(6, 5, 1)}, {"rd_wdata": {}}),
    "mem_handshake_drop": ({"insn": (1 << 12) | (2 << 20) | (1 << 15) | (2 << 7) | 0x23}, {"mem_wmask": {}}),
}


@need
class TestRepo(unittest.TestCase):
    def test_golden_is_upstream(self):
        up = HW / "upstream_picorv32.v"
        if not up.exists(): self.skipTest("no upstream file")
        self.assertEqual((HW / "picorv32_golden.sv").read_text(), up.read_text(),
                         "golden differs from upstream: run tools/apply_fixes.py")

    def test_markers_unique(self):
        g = (HW / "picorv32_golden.sv").read_text()
        for bid, e in json.loads((HW / "bug_manifest.json").read_text()).items():
            self.assertEqual(g.count(e["line_marker"]), 1, bid)

    def test_localisation_contains_every_injected_fault(self):
        g = (HW / "picorv32_golden.sv").read_text()
        for bid, e in json.loads((HW / "bug_manifest.json").read_text()).items():
            buggy = g.replace(e["line_marker"], e["replacement"], 1)
            line = buggy[:buggy.find(e["replacement"])].count("\n") + 1
            actual, diffs = SIGNATURE[bid]
            loc = localize_rtl_fault(buggy, {"actual": actual, "differences": diffs})
            self.assertTrue(any(c["start_line"] <= line <= c["end_line"] for c in loc["candidates"]),
                            f"{bid}: fault line {line} not in {[(c['start_line'], c['end_line']) for c in loc['candidates']]}")


if __name__ == "__main__":
    unittest.main()
