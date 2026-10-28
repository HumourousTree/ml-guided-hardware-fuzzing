"""Bug injection by exact text replacement into a working copy of the RTL.

Manifests: bug_manifest.json (legacy, single line_marker/replacement per bug) and bug_manifest_v2.json
(tiered suite; every bug is a list of `edits`, each {marker, replacement}).  Every marker must occur
exactly once in the golden file; multi-bug injection fails (and restores golden) if edits collide.
"""
import json, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HW_DIR = ROOT / "hw_design"
MUTABLE_RTL = HW_DIR / "picorv32_mutable.sv"
GOLDEN_RTL = HW_DIR / "picorv32_golden.sv"
MANIFESTS = {"legacy": HW_DIR / "bug_manifest.json", "v2": HW_DIR / "bug_manifest_v2.json"}
ACTIVE_BUGS_FILE = HW_DIR / ".active_bugs.json"


def _normalise(entry: dict) -> dict:
    entry = dict(entry)
    if "edits" not in entry:
        entry["edits"] = [{"marker": entry["line_marker"], "replacement": entry["replacement"]}]
    entry["replacement"] = entry["edits"][0]["replacement"]       # kept for the fault-line lookup
    return entry


def load_manifest(suite: str = "legacy") -> dict:
    """suite: 'legacy', 'v2' or 'all'."""
    names = ["legacy", "v2"] if suite == "all" else [suite]
    merged: dict = {}
    for n in names:
        if MANIFESTS[n].exists():
            for k, v in json.loads(MANIFESTS[n].read_text()).items():
                merged[k] = dict(_normalise(v), suite=n)
    if not merged:
        raise FileNotFoundError(f"no bug manifest found for suite '{suite}'")
    return merged


def get_active_bugs() -> list:
    if ACTIVE_BUGS_FILE.exists():
        try:
            return json.loads(ACTIVE_BUGS_FILE.read_text()).get("active_bugs", [])
        except Exception:
            return []
    return []


def _set_active_bugs(ids: list) -> None:
    ACTIVE_BUGS_FILE.write_text(json.dumps({"active_bugs": ids}, indent=2))


def restore_golden() -> bool:
    if not GOLDEN_RTL.exists():
        raise FileNotFoundError(GOLDEN_RTL)
    shutil.copy2(GOLDEN_RTL, MUTABLE_RTL)
    _set_active_bugs([])
    return True


def check_manifest_against(golden_text: str, suite: str = "all") -> list[str]:
    """Problems (empty list = fine): every marker must be present exactly once in the golden text."""
    problems = []
    for bid, e in load_manifest(suite).items():
        for i, ed in enumerate(e["edits"]):
            n = golden_text.count(ed["marker"])
            if n != 1:
                problems.append(f"{bid} edit {i}: marker found {n} times")
    return problems


def injected_text(bug_ids: list, suite: str = "all", golden_text: str | None = None) -> str | None:
    """Golden RTL with the bugs applied, without touching any file (safe for parallel runs); None if an edit fails."""
    manifest = load_manifest(suite)
    for b in bug_ids:
        if b not in manifest:
            raise KeyError(f"Unknown bug '{b}'")
    content = GOLDEN_RTL.read_text() if golden_text is None else golden_text
    for b in bug_ids:
        for ed in manifest[b]["edits"]:
            if content.count(ed["marker"]) != 1:      # missing, ambiguous, or consumed by another injected bug
                return None
            content = content.replace(ed["marker"], ed["replacement"], 1)
    return content


def inject_multiple(bug_ids: list, suite: str = "all") -> bool:
    restore_golden()
    content = injected_text(bug_ids, suite)
    if content is None:
        return False
    MUTABLE_RTL.write_text(content)
    _set_active_bugs(list(bug_ids))
    return True


def inject_bug(bug_id: str, suite: str = "all") -> bool:
    return inject_multiple([bug_id], suite)


def list_bugs(suite: str = "all") -> dict:
    manifest, active = load_manifest(suite), set(get_active_bugs())
    for k, v in manifest.items():
        v["active"] = k in active
    return manifest


def edit_lines(entry: dict, golden_text: str) -> list[int]:
    """1-based line (in the *buggy* file) where each edit lands; edits applied in order, as inject_multiple does."""
    text, out = golden_text, []
    for ed in entry["edits"]:
        out.append(text[:text.find(ed["marker"])].count("\n") + 1)
        text = text.replace(ed["marker"], ed["replacement"], 1)
    return out
