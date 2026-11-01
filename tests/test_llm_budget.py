"""Free-tier request budget: daily cap, minimum interval, stop on 429, day rollover, call log (fake clock, temp files)."""
import json
import tempfile
import unittest
from pathlib import Path

from harness.llm_budget import LLMBudget


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s); self.t += s


def budget(limit=3, interval=10.0):
    d = Path(tempfile.mkdtemp())
    clk = FakeClock()
    return LLMBudget(d / "b.json", d / "calls.jsonl", daily_limit=limit, min_interval_s=interval, clock=clk, sleep=clk.sleep), clk, d


class TestLLMBudget(unittest.TestCase):
    def test_daily_cap_per_model(self):
        b, clk, _ = budget(limit=2, interval=0)
        self.assertTrue(b.acquire("m1")[0]); self.assertTrue(b.acquire("m1")[0])
        ok, reason = b.acquire("m1")
        self.assertFalse(ok); self.assertIn("2/2", reason)
        self.assertTrue(b.acquire("m2")[0])                      # other model has its own quota

    def test_minimum_interval_is_enforced(self):
        b, clk, _ = budget(interval=10)
        b.acquire("m"); clk.t += 3; b.acquire("m")
        self.assertAlmostEqual(clk.slept[-1], 7.0)

    def test_quota_error_blocks_model_for_the_day(self):
        b, clk, d = budget(limit=10, interval=0)
        b.acquire("m"); b.record("m", False, "rtl_repair", 100, 0, "ClientError: 429 RESOURCE_EXHAUSTED")
        ok, reason = b.acquire("m")
        self.assertFalse(ok); self.assertIn("quota", reason)
        log = [json.loads(l) for l in (d / "calls.jsonl").read_text().splitlines()]
        self.assertEqual((log[0]["model"], log[0]["purpose"], log[0]["ok"]), ("m", "rtl_repair", False))

    def test_counters_reset_on_a_new_pacific_day(self):
        b, clk, _ = budget(limit=1, interval=0)
        b.acquire("m"); self.assertFalse(b.acquire("m")[0])
        clk.t += 86400
        self.assertTrue(b.acquire("m")[0])

    def test_state_persists_across_instances(self):
        b, clk, d = budget(limit=2, interval=0)
        b.acquire("m")
        b2 = LLMBudget(d / "b.json", d / "calls.jsonl", daily_limit=2, min_interval_s=0, clock=clk, sleep=clk.sleep)
        self.assertEqual(b2.status()["used"], {"m": 1})


if __name__ == "__main__":
    unittest.main()
