import random, unittest
from sw_reference.golden_rv32i import RV32IReference, CoverageTracker, s32, u32
from harness import riscv_cosim as cosim
from harness.delta_debugger import (slice_to_minimal_reproducer, ddmin_reproducer, localize_rtl_fault,
                                    parse_block_patches, apply_block_patches, prefix_program)
from tests.mock_dut import *


def run(words):
    r = RV32IReference(words); evs = []
    while r.pc < 4 * len(words): evs.append(r.step())
    return r, evs


class TestISS(unittest.TestCase):
    def test_division_exact(self):
        for a, b in [(7, -2), (-7, 2), (-7, -2), (0x80000000, 0xFFFFFFFF), (123456789, 0), (2**31 - 1, 1)]:
            r = RV32IReference([rtype(1, 2, 1, 4, 3), rtype(1, 2, 1, 6, 4)])
            r.regs[1], r.regs[2] = u32(a), u32(b)
            r.step(); r.step()
            sa, sb = s32(a), s32(b)
            if sb == 0: q, m = -1, sa
            elif sa == -2**31 and sb == -1: q, m = -2**31, 0
            else:
                q = abs(sa) // abs(sb) * (-1 if (sa < 0) != (sb < 0) else 1); m = sa - q * sb
            self.assertEqual(r.regs[3], u32(q)); self.assertEqual(r.regs[4], u32(m))

    def test_coverage_ignores_non_writes(self):
        c = CoverageTracker(); c.sample_instruction(sw(1, 2, 0), 0, wrote_rd=False)
        self.assertEqual(c.pattern_counts["all_zeros"], 0)


class TestValidator(unittest.TestCase):
    def ok(self, w): return cosim.analyze_program(w)["ok"]
    def test_good(self): self.assertTrue(self.ok([addi(1, 0, 4), sw(1, 0, 0x100), NOP]))
    def test_funct7_sll_invalid(self): self.assertFalse(self.ok([rtype(0x20, 1, 1, 1, 1), NOP]))
    def test_overlap_program(self): self.assertFalse(self.ok([addi(1, 0, 4), sw(1, 0, 4), NOP]))
    def test_out_of_memory(self): self.assertFalse(self.ok([0x000100b7, sw(1, 1, 0), NOP]))
    def test_unaligned(self): self.assertFalse(self.ok([addi(1, 0, 0x101), lw(2, 1, 0), NOP]))
    def test_infinite_loop(self): self.assertFalse(self.ok([beq(0, 0, 0), NOP]))
    def test_leaves_program(self): self.assertFalse(self.ok([beq(0, 0, 400), NOP]))
    def test_misaligned_target(self): self.assertFalse(self.ok([beq(0, 0, 6), NOP, NOP]))
    def test_div_allowed(self): self.assertTrue(self.ok([addi(1, 0, 9), addi(2, 0, 2), rtype(1, 2, 1, 4, 3), NOP]))


class TestScoreboard(unittest.TestCase):
    prog = [addi(1, 0, 0x100), addi(2, 0, 77), sw(2, 1, 5 - 1), lb(3, 1, 5), addi(4, 3, 1), NOP]
    def test_clean_match_with_aligned_mem_addr(self):
        p = program(self.prog); res = compare_rvfi_trace(p, dut_trace(self.prog))
        self.assertTrue(res["ok"], res["mismatches"])
    def test_unaligned_dut_address_is_flagged(self):
        t = dut_trace(self.prog); t[3]["mem_addr"] += 1
        self.assertFalse(compare_rvfi_trace(program(self.prog), t)["ok"])
    def test_missing_retire_detected(self):
        res = compare_rvfi_trace(program(self.prog), dut_trace(self.prog, max_retire=3), hardware_trap=True)
        self.assertEqual(res["mismatches"][0]["kind"], "missing_retire"); self.assertFalse(res["ok"])
    def test_extra_retire_detected(self):
        t = dut_trace(self.prog); t.append(dict(t[-1], order=99))
        self.assertEqual(compare_rvfi_trace(program(self.prog), t)["mismatches"][0]["kind"], "extra_retire")


class TestDeltaDebug(unittest.TestCase):
    def test_reproducer_property(self):
        fails = fails_on(XoriOrDut); checked = 0
        for seed in range(60):
            w = random_program(random.Random(seed))
            p = program(w)
            if not cosim.analyze_program(w)["ok"] or not fails(p): continue
            cmp = compare_rvfi_trace(p, dut_trace(w, XoriOrDut))
            rep, K, _ = slice_to_minimal_reproducer(p, cmp, validator=lambda q: cosim.validate_program(q)["valid"])
            self.assertTrue(fails(rep), f"seed {seed}: reproducer ({rep['metadata']['mode']}) does not fail")
            mn, log = ddmin_reproducer(rep, fails)
            self.assertTrue(fails(mn))
            self.assertLessEqual(len(mn["words"]), len(rep["words"]))
            checked += 1
        self.assertGreater(checked, 10)

    def test_taken_branch_before_failure_uses_dynamic_trace(self):
        w = [addi(1, 0, 15), beq(0, 0, 8), addi(5, 0, 99), xori(2, 1, -1), NOP]
        p = program(w); cmp = compare_rvfi_trace(p, dut_trace(w, XoriOrDut))
        rep, K, _ = slice_to_minimal_reproducer(p, cmp)
        self.assertEqual(K, 2)
        self.assertTrue(fails_on(XoriOrDut)(rep))

    def test_memory_preload(self):
        w = [addi(1, 0, 0x100), addi(2, 0, -1), sw(2, 1, 0), addi(7, 0, 1), addi(8, 0, 2), lw(3, 1, 0), xori(4, 3, -1), NOP]
        p = program(w); cmp = compare_rvfi_trace(p, dut_trace(w, XoriOrDut)); K = cmp["mismatches"][0]["retire_order"]
        self.assertEqual(K, 6)
        rep, _, _ = slice_to_minimal_reproducer(p, cmp, keep_hazard_context=True)
        # the window (lw, xori) needs memory contents: reproducer must still compute the same ISS result
        r_orig, _ = run([int(x, 0) for x in p["words"]]); r_rep, _ = run([int(x, 0) for x in rep["words"]])
        self.assertEqual(r_orig.regs[3], r_rep.regs[3])

    def test_off_by_one_state(self):
        # instr K-3 sets x1; if the snapshot were taken before it, the xori would see x1=0
        w = [addi(1, 0, 15), addi(6, 0, 1), addi(7, 0, 2), xori(2, 1, -1), NOP]
        p = program(w); cmp = compare_rvfi_trace(p, dut_trace(w, XoriOrDut))
        rep, K, _ = slice_to_minimal_reproducer(p, cmp, keep_hazard_context=True)
        self.assertEqual(K, 3)
        r, _ = run([int(x, 0) for x in rep["words"]]); self.assertEqual(r.regs[2], u32(~15))

    def test_prefix_program(self):
        p = program([NOP] * 10); self.assertGreaterEqual(len(prefix_program(p, 3, 40)["words"]), 4)


SYN = "\n".join(["// header"] * 10 + ["\tgenerate if (TWO_CYCLE_ALU) begin", "x", "\talu_lts = $signed(a) < $signed(b);"] + ["y"] * 60)
class TestLocalizeAndPatch(unittest.TestCase):
    def test_branch_localizes_to_alu_comparator(self):
        loc = localize_rtl_fault(SYN, {"actual": {"insn": beq(1, 2, 8) | 0x4000}, "differences": {"pc_wdata": {}}})
        c = loc["candidates"][0]
        self.assertEqual(loc["subsystem"], "branch"); self.assertLessEqual(c["start_line"], 12 + 1 - 0); self.assertGreaterEqual(c["end_line"], 12)
    def test_patch_roundtrip(self):
        rtl = "\n".join(f"l{i}" for i in range(1, 21))
        txt = "```verilog\n// REPLACE LINES 5-6\nA\nB\nC\n```"
        out = apply_block_patches(rtl, parse_block_patches(txt), [(1, 20)]).splitlines()
        self.assertEqual(out[4:7], ["A", "B", "C"]); self.assertEqual(len(out), 21)
    def test_patch_outside_allowed_rejected(self):
        with self.assertRaises(ValueError): apply_block_patches("a\nb\nc", [(1, 3, "x")], [(2, 2)])


class TestCoverageDat(unittest.TestCase):
    def test_parse_and_merge(self):
        import tempfile, os
        k = lambda page, n: f"\x01f\x02a.v\x01l\x02{n}\x01page\x02{page}/picorv32\x01o\x02x"
        d = tempfile.mkdtemp(); a, b = os.path.join(d, "a.dat"), os.path.join(d, "b.dat")
        open(a, "w").write("# SystemC::Coverage-3\nC '%s' 3\nC '%s' 0\nC '%s' 0\n" % (k("v_line", 1), k("v_line", 2), k("v_toggle", 3)))
        open(b, "w").write("C '%s' 1\nC '%s' 5\n" % (k("v_line", 2), k("v_toggle", 3)))
        cum = {}
        s1 = cosim.summarize_verilator_coverage([a], cum); self.assertEqual(s1["line_coverage_pct"], 50.0)
        s2 = cosim.summarize_verilator_coverage([b], cum); self.assertEqual((s2["line_coverage_pct"], s2["toggle_coverage_pct"]), (100.0, 100.0))


if __name__ == "__main__":
    unittest.main()
