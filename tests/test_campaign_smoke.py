"""End-to-end control-flow test of the campaign with a software DUT and a fake LLM (no Verilator/Gemini needed)."""
import json, re, tempfile, unittest
from pathlib import Path
from unittest import mock
from harness import pico_campaign as pc
from harness.riscv_cosim import validate_program
from tests.mock_dut import dut_trace, XoriOrDut, RV32IReference
from harness.delta_debugger import disassemble_rv32i

RTL = "\n".join(["// filler"] * 30 + ["module picorv32 ();", "\tgenerate if (TWO_CYCLE_ALU) begin", "\t\talu_lts = $signed(a) < $signed(b);"]
                + ["\t\talu_out = a ^ b; // BUG"] + ["// pad"] * 70 + ["endmodule"] + ["//x"] * 800)


def fake_sim(rtl_path, prog, workdir, iteration=0, generate_vcd=True, mem_delay=0, vcd_name=None):
    val = validate_program(prog)
    if not val["valid"]:
        return {"ok": False, "error": val["reason"], "hardware_trace": [], "retired": 0, "cycles": 0, "cpi": 0, "validation": val}
    words = [int(w, 0) for w in prog["words"]]
    cls = XoriOrDut if "BUG" in Path(rtl_path).read_text() else RV32IReference
    tr = dut_trace(words, cls)
    return {"ok": True, "hardware_trace": tr, "hardware_trap": False, "retired": len(tr), "cycles": 9 * len(tr), "cpi": 9.0,
            "vcd_path": None, "coverage_path": None, "validation": val, "lint_log": ""}


class FakeClient:
    def __init__(self): self.prompts = []
    chats = None


def fake_generate(client, models, prompt):
    client.prompts.append(prompt)
    m = re.search(r"\(lines (\d+)-(\d+)\)", prompt)
    a, b = int(m.group(1)), int(m.group(2))
    block = re.search(r"```verilog\n(.*?)```", prompt, re.S).group(1)
    fixed = "\n".join(re.sub(r"^\s*\d+: ", "", l).replace("a ^ b; // BUG", "a ^ b;") for l in block.splitlines())
    return f"```verilog\n// REPLACE LINES {a}-{b}\n{fixed}\n```", "fake-model", []


class TestCampaign(unittest.TestCase):
    def test_detect_repair_and_telemetry(self):
        d = Path(tempfile.mkdtemp())
        (d / "hw_design").mkdir(); (d / "logs").mkdir(); (d / "test_corpus").mkdir()
        hw = d / "hw_design" / "picorv32_mutable.sv"; hw.write_text(RTL)
        client = FakeClient()
        patches = dict(HW=hw, HW_ALT=d / "hw_design" / "picorv32.v", LOGS=d / "logs", TELEMETRY=d / "logs/t.json",
                       TELEMETRY_ALT=d / "logs/t2.json", CHECKPOINTS=d / "logs/ck", STOP_FILE=d / "logs/stop",
                       RUN_STATE=d / "logs/pid", PROGRAMS=d / "test_corpus/p.json", PROGRAMS_ALT=d / "test_corpus/p2.json",
                       HIGH_YIELD=d / "test_corpus/hy", GOLDEN_CHECKPOINT=d / "none.v", HW_GOLDEN_SV=d / "none.sv")
        with mock.patch.multiple(pc, build_and_simulate_program=fake_sim, run_verilator_lint=lambda **k: (True, ""),
                                 estimate_yosys_stats=lambda *a, **k: {"total_cells": 5},
                                 get_active_bugs=lambda: ["alu_operator_xor_or"],
                                 _get_gemini_client=lambda: (client, ["fake"]),
                                 _generate_with_gemini=fake_generate, **patches):
            (d / "logs/ck").mkdir()
            res = pc.run_pico_campaign(seconds=60, max_iterations=3, fuzz_interval=50)
        self.assertEqual(res["status"], "completed", res.get("error"))
        it = res["iterations"]
        self.assertGreater(it[0]["mismatch_total"], 0); self.assertIn("Repair", it[0]["mode"])   # detected on iteration 1
        self.assertEqual(it[0]["llm_source"].split(":")[0], "gemini_block_patch")
        self.assertTrue(it[0]["rtl_improved"])
        self.assertNotIn("BUG", hw.read_text())                   # patch spliced in
        self.assertTrue(it[1]["comparison_ok"])                   # stays fixed
        self.assertEqual(res["rtl_improvement_count"], 1)
        self.assertEqual(res["golden_recovery_count"], 0)
        dd = it[0]["delta_debug_info"]
        self.assertTrue(dd["slice_reproduces_failure"]); self.assertGreater(len(dd["shrink_phases"]), 0)
        self.assertEqual(dd["localized_subsystem"], "alu")
        self.assertLessEqual(dd["minimal_program_length"], 6)
        self.assertEqual(res["hardware_config"], {"ENABLE_FAST_MUL": True, "ENABLE_DIV": True, "REGS_INIT_ZERO": True})
        self.assertEqual(it[0]["llm_called"], True); self.assertEqual(it[1]["llm_called"], False)

    def test_no_key_never_counts_golden_as_repair(self):
        d = Path(tempfile.mkdtemp()); hw = d / "m.sv"; hw.write_text(RTL)
        with mock.patch.multiple(pc, HW=hw, HW_ALT=d / "a.v", _get_gemini_client=lambda: (None, None)):
            cand, src, n, info = pc._llm_repair_hardware_with_delta_debug({}, {"id": "x", "words": []}, {"mismatches": []}, d)
        self.assertIsNone(cand); self.assertEqual(src, "no_api_key")

    def test_bug_targeted_programs_valid(self):
        for b in ["approx_multiplier", "alu_operator_xor_or", "branch_inversion_blt", "writeback_forwarding_drop", "mem_handshake_drop"]:
            p = pc._generate_bug_targeted_stimulus(b); self.assertTrue(validate_program(p)["valid"], (b, validate_program(p)))

    def test_seed_programs_valid_and_fuzz_yield(self):
        for p in pc.DEFAULT_SEED_PROGRAMS: self.assertTrue(validate_program(p)["valid"], validate_program(p))
        import random; rng = random.Random(1); log = []
        for p in pc.DEFAULT_SEED_PROGRAMS * 20: pc._directed_mutate_program(p, rng, 1, log)
        self.assertTrue(all(validate_program(_p)["valid"] for _p in [pc._directed_mutate_program(p, rng, 1, []) for p in pc.DEFAULT_SEED_PROGRAMS]))

    def test_reg_alias_keeps_funct3(self):
        import random
        rng = random.Random(0); p = pc.DEFAULT_SEED_PROGRAMS[0]
        for _ in range(300):
            c, t = pc._generate_candidate_mutation(p, rng, 1)
            if t == "reg_alias":
                for o, n in zip(p["words"], c["words"]):
                    o, n = int(o, 0), int(n, 0)
                    if o != n and (o & 0x7F) == 0x33: self.assertEqual((o >> 12) & 7, (n >> 12) & 7)

    def test_corpus_pins_seeds(self):
        progs = list(pc.DEFAULT_SEED_PROGRAMS)
        for i in range(10): progs = pc._merge_corpus(progs, {"id": f"g{i}", "words": []})
        self.assertEqual(len(progs), pc.CORPUS_CAP); self.assertTrue(pc.PINNED_SEED_IDS <= {p["id"] for p in progs})


if __name__ == "__main__":
    unittest.main()
