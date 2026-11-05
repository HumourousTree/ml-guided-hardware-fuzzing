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
    return LLMBudget(d / "b.json", d / "calls.jsonl", daily_limit=limit, min_interval_s=interval, clock=clk, sleep=clk.sleep,
                     model_limits={}), clk, d


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
        self.assertEqual(list(b2.status()["used"].values()), [1])

    def test_per_model_limits_and_intervals(self):
        d = Path(tempfile.mkdtemp()); clk = FakeClock()
        b = LLMBudget(d / "b.json", d / "c.jsonl", daily_limit=1, min_interval_s=100, clock=clk, sleep=clk.sleep,
                      model_limits={"fast": (3, 2.0)})
        for _ in range(3):
            self.assertTrue(b.acquire("fast")[0])
        self.assertFalse(b.acquire("fast")[0])                    # its own cap (3), not the generic one (1)
        self.assertTrue(all(abs(x - 2.0) < 1e-9 for x in clk.slept))  # its own interval (2 s), not 100 s

    def test_counters_are_per_api_key(self):
        import os
        from unittest import mock
        b, clk, _ = budget(limit=1, interval=0)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key-A"}):
            self.assertTrue(b.acquire("m")[0]); self.assertFalse(b.acquire("m")[0])
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key-B"}):
            self.assertTrue(b.acquire("m")[0])
        self.assertNotIn("key-A", (b.state_file).read_text())     # only a hash is stored

    # real 429 texts from the API (2026-11-05), shortened
    MINUTE = ("ClientError: 429 RESOURCE_EXHAUSTED. You exceeded your current quota ... * Quota exceeded for metric: "
              "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 15, model: gemini-3.5-flash-lite, "
              "description: Generate content requests per minute limit exceeded on Free Tier.\\nPlease retry in 9.980955495s.")
    ZERO = ("ClientError: 429 RESOURCE_EXHAUSTED. * Quota exceeded for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 0, model: gemini-3.1-pro, description: Generate content requests per "
            "day limit exceeded on Free Tier. * Quota exceeded for metric: ..._input_token_count, limit: 0, model: gemini-3.1-pro,"
            " description: Generate content input tokens per minute limit exceeded on Free Tier.")

    def test_per_minute_429_does_not_block_the_day(self):
        from harness.llm_budget import quota_kind, retry_delay_s
        self.assertEqual(quota_kind(self.MINUTE), "minute")
        self.assertAlmostEqual(retry_delay_s(self.MINUTE), 11.980955495)
        b, clk, d = budget(limit=10, interval=0)
        b.acquire("m"); b.record("m", False, "x", 1, 0, self.MINUTE)
        self.assertTrue(b.acquire("m")[0])
        self.assertIn("limit: 15", json.loads((d / "calls.jsonl").read_text().splitlines()[0])["error"])

    def test_daily_or_zero_quota_429_blocks_the_day(self):
        from harness.llm_budget import quota_kind
        self.assertEqual(quota_kind(self.ZERO), "day")
        self.assertEqual(quota_kind("429 RESOURCE_EXHAUSTED"), "day")
        self.assertIsNone(quota_kind("503 UNAVAILABLE"))


class TestGeminiClientReuse(unittest.TestCase):
    def test_one_client_per_key(self):
        import os
        from unittest import mock
        from harness import pico_campaign as pc
        if not pc._GENAI_AVAILABLE:
            self.skipTest("google-genai not installed")
        pc._GEMINI_CLIENT.clear()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-not-real"}):
            c1, _ = pc._get_gemini_client()
            pc._GEMINI_CHATS["m"] = object()
            c2, _ = pc._get_gemini_client()
        self.assertIs(c1, c2)                       # a second call must not create (and later close) another client
        self.assertIn("m", pc._GEMINI_CHATS)        # cached chats survive while the client does
        pc._GEMINI_CLIENT.clear(); pc._GEMINI_CHATS.clear()


if __name__ == "__main__":
    unittest.main()
