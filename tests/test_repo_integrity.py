"""Checks against the real RTL/manifest in the repository (skipped when the files are absent)."""
import json, unittest
from pathlib import Path
from harness.delta_debugger import localize_rtl_fault
from tests.mock_dut import xori, addi, rtype, sw, lw, beq, NOP

HW = Path(__file__).resolve().parents[1] / "hw_design"
need = unittest.skipUnless((HW / "picorv32_golden.sv").exists() and (HW / "bug_manifest.json").exists(), "RTL not present")

# an RVFI failing-instruction signature for each bug in bug_manifest.json
from tests.mock_dut import addi as _addi
SIGNATURE_V2 = {   # failing-instruction signatures the DUT would present (see run_experiments for the measured ones)
    "and_to_or_alu": ({"insn": (7 << 12) | (2 << 20) | (1 << 15) | (3 << 7) | 0x33}, {"rd_wdata": {}}),
    "mem_addr_bit2_stuck": ({"insn": (2 << 12) | (1 << 15) | (3 << 7) | 0x03 | (4 << 20)}, {"mem_addr": {}}),
    "slt_signed_as_unsigned": ({"insn": (2 << 12) | (2 << 20) | (1 << 15) | (3 << 7) | 0x33}, {"rd_wdata": {}}),
    "beq_inverted": ({"insn": beq(1, 2, 8)}, {"pc_wdata": {}}),
    "lui_auipc_rd_x0": ({"insn": (0x12345 << 12) | (1 << 7) | 0x37}, {"rd_addr": {}}),
    "store_after_store_dropped": ({"insn": (2 << 12) | (2 << 20) | (1 << 15) | (4 << 7) | 0x23}, {"mem_wmask": {}}),
    "stale_latched_branch": ({"insn": _addi(3, 0, 3)}, {"pc_wdata": {}}),
    "fast_mul_rs2_signed_widened": ({"insn": rtype(1, 2, 1, 2, 3)}, {"rd_wdata": {}}),
    "store_after_store_dropped_intermittent": ({"insn": (2 << 12) | (2 << 20) | (1 << 15) | (4 << 7) | 0x23}, {"mem_wmask": {}}),
}
UNOBSERVABLE_V2 = {"fast_mul_decode_and_to_or"}   # equivalent mutant (E1: never detected), so it has no failure signature
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
                         "golden differs from upstream: copy tools/fetch_upstream.py's output over picorv32_golden.sv")

    def test_markers_unique(self):
        g = (HW / "picorv32_golden.sv").read_text()
        for bid, e in json.loads((HW / "bug_manifest.json").read_text()).items():
            self.assertEqual(g.count(e["line_marker"]), 1, bid)

    def test_v2_manifest_markers_unique_and_localised(self):
        from hw_design.bug_injector import load_manifest, check_manifest_against
        g = (HW / "picorv32_golden.sv").read_text()
        self.assertEqual(check_manifest_against(g, "all"), [])
        for bid, e in load_manifest("v2").items():
            if bid in UNOBSERVABLE_V2:
                continue
            buggy, lines_of_edits = g, []
            for ed in e["edits"]:            # position of each edit = position of its (unique) marker, applied in order
                lines_of_edits.append(buggy[:buggy.find(ed["marker"])].count("\n") + 1)
                buggy = buggy.replace(ed["marker"], ed["replacement"], 1)
            # a multi-edit bug is "localised" if at least one of its edits is inside an offered block
            actual, diffs = SIGNATURE_V2[bid]
            loc = localize_rtl_fault(buggy, {"actual": actual, "differences": diffs})
            ranges = [(c["start_line"], c["end_line"]) for c in loc["candidates"]]
            print(f"  {bid:28s} edits@{lines_of_edits} subsystem={loc['subsystem']:14s} blocks={ranges}")
            self.assertTrue(any(a <= l <= b for l in lines_of_edits for a, b in ranges), f"{bid}: {lines_of_edits} not in {ranges}")

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
