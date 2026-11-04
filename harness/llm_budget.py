"""Strict request budget for the free-tier Gemini key.

Every real LLM request goes through `LLMBudget.acquire(model)` / `record(...)`:
  * a daily cap and a minimum gap between requests per model (MODEL_LIMITS below, overridable with
    GEMINI_LIMITS="model:daily:interval_s,..."; other models use GEMINI_DAILY_LIMIT / GEMINI_MIN_INTERVAL_S, default 15 / 10 s),
    persisted in logs/llm_budget.json so they survive restarts and are shared by every tool;
  * counters are kept per API key (a short hash, never the key) and per model;
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
# Free-tier limits as stated by the user (RPM / RPD): 3.8-flash 15 / 1500, 3.5-flash-lite 30 / 1500, 3.1-pro-preview 2 / 50.
# Caps keep ~20% headroom; intervals keep the request rate below the per-minute limit.
MODEL_LIMITS = {"gemini-3.8-flash": (1200, 5.0), "gemini-3.5-flash-lite": (1200, 2.5), "gemini-3.1-pro-preview": (40, 35.0)}


def _key_id() -> str:
    import hashlib
    key = os.environ.get("GEMINI_API_KEY", "")
    return hashlib.sha256(key.encode()).hexdigest()[:8] if key else "nokey"


class LLMBudget:
    def __init__(self, state_file: Path | None = None, call_log: Path | None = None, daily_limit: int | None = None,
                 min_interval_s: float | None = None, clock=time.time, sleep=time.sleep, model_limits: dict | None = None):
        self.state_file = Path(state_file or os.environ.get("GEMINI_BUDGET_FILE", ROOT / "logs" / "llm_budget.json"))
        self.call_log = Path(call_log or self.state_file.with_name("llm_calls.jsonl"))
        self.daily_limit = int(daily_limit if daily_limit is not None else os.environ.get("GEMINI_DAILY_LIMIT", 15))
        self.min_interval_s = float(min_interval_s if min_interval_s is not None else os.environ.get("GEMINI_MIN_INTERVAL_S", 10))
        self.model_limits = dict(MODEL_LIMITS if model_limits is None else model_limits)
        for item in filter(None, os.environ.get("GEMINI_LIMITS", "").split(",")):
            m, daily, gap = item.rsplit(":", 2)
            self.model_limits[m.strip()] = (int(daily), float(gap))
        self.clock, self.sleep = clock, sleep

    def limits(self, model: str) -> tuple[int, float]:
        return self.model_limits.get(model, (self.daily_limit, self.min_interval_s))

    @staticmethod
    def _slot(model: str) -> str:
        return f"{_key_id()}:{model}"

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
                state = {"day": self._day(), "used": {}, "exhausted": [], "last_call": state.get("last_call", {})}
            if not isinstance(state.get("last_call"), dict):                # older single-timestamp format
                state["last_call"] = {}
            yield state
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1))
            os.replace(tmp, self.state_file)

    def acquire(self, model: str) -> tuple[bool, str]:
        """Reserve one request for `model`; waits for the minimum interval. Returns (allowed, reason)."""
        slot, (limit, gap) = self._slot(model), self.limits(model)
        with self._locked_state() as st:
            if slot in st["exhausted"]:
                return False, f"{model} hit its provider quota today ({st['day']} Pacific)"
            used = st["used"].get(slot, 0)
            if used >= limit:
                return False, f"{model} used {used}/{limit} requests today ({st['day']} Pacific)"
            wait = st["last_call"].get(slot, 0.0) + gap - self.clock()
            if wait > 0:
                self.sleep(wait)
            st["used"][slot] = used + 1
            st["last_call"][slot] = self.clock()
            return True, f"request {used + 1}/{limit} for {model}"

    def record(self, model: str, ok: bool, purpose: str = "", prompt_chars: int = 0, response_chars: int = 0,
               error: str | None = None) -> None:
        if error and any(m in error for m in QUOTA_MARKERS):
            with self._locked_state() as st:
                if self._slot(model) not in st["exhausted"]:
                    st["exhausted"].append(self._slot(model))
        self.call_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.call_log, "a") as f:
            f.write(json.dumps({"time": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), "key": _key_id(), "model": model,
                                "purpose": purpose, "ok": ok, "prompt_chars": prompt_chars,
                                "response_chars": response_chars, "error": (error or "")[:300]}) + "\n")

    def status(self) -> dict:
        with self._locked_state() as st:
            return {"day": st["day"], "key": _key_id(), "used": dict(st["used"]), "exhausted": list(st["exhausted"]),
                    "limits": {m: {"per_day": d, "min_interval_s": g} for m, (d, g) in self.model_limits.items()}}


BUDGET = LLMBudget()


if __name__ == "__main__":
    import harness.pico_campaign  # noqa: F401  (loads GEMINI_API_KEY from ~/.bashrc, so the key hash is shown)
    print(json.dumps(BUDGET.status(), indent=1))
