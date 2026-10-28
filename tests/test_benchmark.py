"""Logic tests for the benchmark framework using a deterministic mock backend (NOT a source of coverage results)."""
import random, unittest
from harness import benchmark as bm
from harness.benchmark import RandomGen, MutationGen, HybridGen, RunResult, run_campaign
from harness.semantic_gen import SemanticGenerator, uncovered_line_segments, decode_point, build_prompt, extract_asm
from harness.riscv_cosim import analyze_program
from sw_reference.golden_rv32i import RV32IReference


def key(n, page="v_line"): return f"\x01f\x02rtl.v\x01l\x02{n}\x01page\x02{page}/m\x01o\x02x"


def classes(words):
    r, out = RV32IReference(words), set()
    n = 0
    while r.pc < 4 * len(words) and n < 5000:
        ev = r.step(); n += 1; i = ev["insn"]
        out.add((i & 0x7F, (i >> 12) & 7, (i >> 25) == 1))
    return out, n


class MockBackend:
    """Each (opcode, funct3, is_muldiv) class owns 3 RTL lines; classes are numbered by first use order of a fixed table."""
    TABLE = sorted({(op, f3, md) for op in (0x33, 0x13, 0x03, 0x23, 0x63, 0x37, 0x17, 0x6F) for f3 in range(8) for md in (False, True) if (op == 0x33 or not md)})

    def __init__(self):
        self.universe = {key(3 * i + j) for i in range(len(self.TABLE)) for j in range(3)}
        self.runs = 0

    def lines_of(self, cls): i = self.TABLE.index(cls); return {key(3 * i + j) for j in range(3)}

    def run(self, program):
        self.runs += 1
        words = [int(w, 0) for w in program["words"]]
        cl, n = classes(words)
        cov = set().union(*[self.lines_of(c) for c in cl if c in self.TABLE]) if cl else set()
        return RunResult(True, n, cov, False)


class TestFramework(unittest.TestCase):
    def test_random_programs_are_valid_and_deterministic(self):
        a, b = RandomGen(random.Random(5)), RandomGen(random.Random(5))
        for _ in range(40):
            pa, pb = a.next(None)[0], b.next(None)[0]
            self.assertEqual(pa["words"], pb["words"]); self.assertTrue(analyze_program([int(w, 0) for w in pa["words"]])["ok"])

    def test_long_random_programs_valid(self):
        g = RandomGen(random.Random(1), length=300)
        for _ in range(5): self.assertTrue(analyze_program([int(w, 0) for w in g.next(None)[0]["words"]])["ok"])

    def test_campaign_series_is_monotone_and_comparable(self):
        be = MockBackend(); res = run_campaign(be, RandomGen(random.Random(2)), budget_programs=150)
        pct = [s["pct"] for s in res["series"]]; self.assertTrue(all(b >= a for a, b in zip(pct, pct[1:])))
        self.assertEqual(res["series"][-1]["n_programs"], 150); self.assertGreater(pct[-1], 20)
        self.assertEqual(bm.tests_to_threshold(res["series"], pct[-1]) <= 150, True)

    def test_mutation_keeps_only_coverage_increasing_seeds_after_bootstrap(self):
        be, g = MockBackend(), MutationGen(random.Random(3))
        res = run_campaign(be, g, budget_programs=200)
        self.assertTrue(4 <= len(g.corpus) < 200)
        for p in [g.next(None)[0] for _ in range(20)]: self.assertTrue(analyze_program([int(w, 0) for w in p["words"]])["ok"])

    def test_plateau_rule(self):
        self.assertFalse(bm.plateau_reached([1.0] * 50, 100, .1))
        self.assertTrue(bm.plateau_reached([50.0] + [60.0] * 100 + [60.05], 100, .1) or bm.plateau_reached([60.0] * 102, 100, .1))
        self.assertFalse(bm.plateau_reached(list(range(150)), 100, .1))

    def test_hybrid_switches_after_plateau_and_changes_source(self):
        class Stuck:                                  # never finds anything new -> must trigger the switch
            name = "stuck"; n = 0
            def next(self, s):
                self.n += 1; return [{"id": f"s{self.n}", "base": "0x0", "words": ["0x00000013"] * 3, "src": "stuck"}]
            def feedback(self, *a): pass
        h = HybridGen(Stuck(), RandomGen(random.Random(4)), window=20, thresh_pp=0.1)
        res = run_campaign(MockBackend(), h, budget_programs=120)
        self.assertIsNotNone(res["switched_at"]); srcs = [s["src"] for s in res["series"]]
        self.assertEqual(srcs[0], "stuck"); self.assertEqual(srcs[-1], "random")
        self.assertGreater(res["series"][-1]["pct"], res["series"][res["switched_at"]["programs"] - 1]["pct"])

    def test_metrics_and_stats(self):
        s = [{"t": i, "n_programs": i + 1, "pct": 10.0 * (i + 1)} for i in range(5)]
        self.assertEqual((bm.tests_to_threshold(s, 25), bm.time_to_threshold(s, 25), bm.coverage_at(s, 2.5)), (3, 2, 30.0))
        self.assertIsNone(bm.tests_to_threshold(s, 99))
        m, ci = bm.mean_ci95([1, 2, 3, 4, 5]); self.assertAlmostEqual(m, 3.0); self.assertGreater(ci, 0)
        _, p = bm.mann_whitney_u(list(range(1, 9)), list(range(9, 17))); self.assertLess(p, 0.01)
        _, p2 = bm.mann_whitney_u([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]); self.assertGreater(p2, 0.9)


class TestSemantic(unittest.TestCase):
    def setUp(self):
        self.be = MockBackend(); self.rtl = "\n".join(f"line {i}" for i in range(1, 200))

    def test_segments_and_decode(self):
        d = decode_point(key(12)); self.assertEqual((d["kind"], d["l"]), ("v_line", "12"))
        covered = {key(1), key(2), key(3), key(7)}
        U = {key(i) for i in range(1, 9)}
        self.assertEqual(uncovered_line_segments(U, covered), [(4, 8)])                 # default gap tolerance 2 merges 4-6 and 8
        self.assertEqual(uncovered_line_segments(U, covered, max_gap=1), [(4, 6), (8, 8)])

    def test_prompt_contains_target_template_and_constraints(self):
        t = {"words": ["0x00000013"]}; p = build_prompt(self.rtl.splitlines(), (50, 52), t)
        self.assertIn("   50>", p); self.assertIn("nop", p); self.assertIn("RV32IM only", p); self.assertEqual(extract_asm("x\n```asm\nnop\n```"), "nop")

    def test_syntax_then_semantic_correction_then_hit(self):
        div_cls = (0x33, 4, True); div_lines = self.be.TABLE.index(div_cls) * 3
        replies = iter(["```asm\nli x1, 7\ndiv x2, x1\n```",                        # syntax error (missing operand)
                        "```asm\nli x1, 7\nli x2, 2\nadd x3, x1, x2\nnop\n```",    # assembles, misses the target
                        "```asm\nli x1, 7\nli x2, 2\ndiv x3, x1, x2\nnop\n```"])   # hits the target
        prompts = []
        def llm(p): prompts.append(p); return next(replies)
        sg = SemanticGenerator(self.rtl, self.be, llm, random.Random(1))
        # make the div class the only uncovered target
        covered = set().union(*[self.be.lines_of(c) for c in MockBackend.TABLE if c != div_cls])
        ex = sg.generate(covered, "t")
        self.assertEqual(len(prompts), 3); self.assertIn("failed to assemble", prompts[1]); self.assertIn("did not execute", prompts[2])
        self.assertEqual((sg.stats["syntax_fixes"], sg.stats["semantic_fixes"], sg.stats["target_hit"]), (1, 1, 1))
        self.assertTrue(any(k in ex[-1][1].covered for k in self.be.lines_of(div_cls))); self.assertEqual(len(sg.seeds.entries), 2)

    def test_gives_up_and_blacklists_target_after_repeated_failure(self):
        sg = SemanticGenerator(self.rtl, self.be, lambda p: "```asm\nnop\n```", random.Random(2), max_semantic_fix=2, max_target_failures=1)
        covered = set(self.be.universe) - self.be.lines_of((0x33, 4, True))
        sg.generate(covered, "a"); self.assertEqual(sg.stats["gave_up"], 1); self.assertEqual(sg.generate(covered, "b"), [])

    def test_illegal_program_feedback(self):
        replies = iter(["```asm\nli x1, 0\nsw x1, 0(x0)\nnop\n```", "```asm\nnop\n```"]); prompts = []
        def llm(p): prompts.append(p); return next(replies)
        sg = SemanticGenerator(self.rtl, self.be, llm, random.Random(3), max_semantic_fix=0)
        sg.generate(set(self.be.universe) - self.be.lines_of((0x33, 4, True)), "x")
        self.assertIn("not a legal test", prompts[1]); self.assertEqual(sg.stats["illegal"], 1)


if __name__ == "__main__":
    unittest.main()


class TestRepairEvalAndAnalysis(unittest.TestCase):
    def test_pass_at_k_estimator(self):
        from harness.repair_eval import pass_at_k
        self.assertEqual(pass_at_k(5, 0, 1), 0.0); self.assertEqual(pass_at_k(5, 5, 1), 1.0)
        self.assertAlmostEqual(pass_at_k(5, 1, 1), 0.2); self.assertAlmostEqual(pass_at_k(5, 2, 5), 1.0)
        self.assertAlmostEqual(pass_at_k(10, 3, 2), 1 - (21 / 45))            # 1 - C(7,2)/C(10,2)

    def test_analyze_tool_end_to_end_on_mock_runs(self):
        import json, tempfile, subprocess, sys
        d = Path = __import__("pathlib").Path(tempfile.mkdtemp())
        for m, gen in (("random", lambda s: RandomGen(random.Random(s))), ("mutation", lambda s: MutationGen(random.Random(s)))):
            for seed in range(3):
                r = run_campaign(MockBackend(), gen(seed), budget_programs=60); r.update(seed=seed, bug=None)
                (d / f"{m}_seed{seed}.json").write_text(json.dumps(r))
        out = d / "rep"
        res = subprocess.run([sys.executable, "tools/analyze_bench.py", str(d), "--out", str(out), "--thresholds", "20", "30"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        s = json.loads((out / "summary.json").read_text())
        self.assertEqual(set(s["methods"]), {"random", "mutation"}); self.assertIn("mutation vs random", s["pairwise_p"])
        self.assertTrue((out / "table_coverage.tex").exists())
