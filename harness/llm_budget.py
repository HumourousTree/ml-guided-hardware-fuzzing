"""Strict request budget for the free-tier Gemini key.

Every real LLM request goes through `LLMBudget.acquire(model)` / `record(...)`:
  * a per-model daily cap (GEMINI_DAILY_LIMIT, default 15), persisted in logs/llm_budget.json so it survives restarts and is
    shared by every tool (campaign, benchmark, repair evaluation);
  * a minimum gap between requests (GEMINI_MIN_INTERVAL_S, default 10 s);
  * a 429 / RESOURCE_EXHAUSTED answer blocks that model until the quota resets, instead of retrying;
  * every request is appended to logs/llm_calls.jsonl (time, model, purpose, prompt/response size, outcome).
The day is counted in US Pacific time because that is when the Gemini free-tier quota resets.
"""
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl          # WSL / Linux: serialise processes that share the budget file
except ImportError:       # pragma: no cover - Windows Python
    fcntl = None

try:
    from zoneinfo import ZoneInfo
    _QUOTA_TZ = ZoneInfo("America/Los_Angeles")
except Exception:         # pragma: no cover - no tz database
    _QUOTA_TZ = timezone.utc

ROOT = Path(__file__).resolve().parents[1]
QUOTA_MARKERS = ("429", "RESOURCE_EXHAUSTED", "quota")


class LLMBudget:
    def __init__(self, state_file: Path | None = None, call_log: Path | None = None, daily_limit: int | None = None,
                 min_interval_s: float | None = None, clock=time.time, sleep=time.sleep):
        self.state_file = Path(state_file or os.environ.get("GEMINI_BUDGET_FILE", ROOT / "logs" / "llm_budget.json"))
        self.call_log = Path(call_log or self.state_file.with_name("llm_calls.jsonl"))
        self.daily_limit = int(daily_limit if daily_limit is not None else os.environ.get("GEMINI_DAILY_LIMIT", 15))
        self.min_interval_s = float(min_interval_s if min_interval_s is not None else os.environ.get("GEMINI_MIN_INTERVAL_S", 10))
        self.clock, self.sleep = clock, sleep

    def _day(self) -> str:
        return datetime.fromtimestamp(self.clock(), _QUOTA_TZ).strftime("%Y-%m-%d")

    @contextmanager
    def _locked_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file.with_suffix(".lock"), "w") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
            except (OSError, json.JSONDecodeError):
                state = {}
            if state.get("day") != self._day():                       # new quota day: counters reset
                state = {"day": self._day(), "used": {}, "exhausted": [], "last_call": state.get("last_call", 0.0)}
            yield state
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1))
            os.replace(tmp, self.state_file)

    def acquire(self, model: str) -> tuple[bool, str]:
        """Reserve one request for `model`; waits for the minimum interval. Returns (allowed, reason)."""
        with self._locked_state() as st:
            if model in st["exhausted"]:
                return False, f"{model} hit its provider quota today ({st['day']} Pacific)"
            used = st["used"].get(model, 0)
            if used >= self.daily_limit:
                return False, f"{model} used {used}/{self.daily_limit} requests today ({st['day']} Pacific)"
            wait = st.get("last_call", 0.0) + self.min_interval_s - self.clock()
            if wait > 0:
                self.sleep(wait)
            st["used"][model] = used + 1
            st["last_call"] = self.clock()
            return True, f"request {used + 1}/{self.daily_limit} for {model}"

    def record(self, model: str, ok: bool, purpose: str = "", prompt_chars: int = 0, response_chars: int = 0,
               error: str | None = None) -> None:
        if error and any(m in error for m in QUOTA_MARKERS):
            with self._locked_state() as st:
                if model not in st["exhausted"]:
                    st["exhausted"].append(model)
        self.call_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.call_log, "a") as f:
            f.write(json.dumps({"time": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), "model": model,
                                "purpose": purpose, "ok": ok, "prompt_chars": prompt_chars,
                                "response_chars": response_chars, "error": (error or "")[:300]}) + "\n")

    def status(self) -> dict:
        with self._locked_state() as st:
            return {"day": st["day"], "limit_per_model": self.daily_limit, "used": dict(st["used"]),
                    "exhausted": list(st["exhausted"])}


BUDGET = LLMBudget()


if __name__ == "__main__":
    print(json.dumps(BUDGET.status(), indent=1))
