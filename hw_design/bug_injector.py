"""Hardware Bug Injection Engine for Differential Fuzzing and Verification Benchmarks.

Supports multi-bug taxonomy across Levels 1-3 including Intel Pentium FDIV-style
approximate multiplier, ALU operator mutation, branch condition inversion,
writeback forwarding hazard, and memory handshake protocol drop.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HW_DIR = ROOT / "hw_design"
MUTABLE_RTL = HW_DIR / "picorv32_mutable.sv"
GOLDEN_RTL = HW_DIR / "picorv32_golden.sv"
MANIFEST_PATH = HW_DIR / "bug_manifest.json"
ACTIVE_BUGS_FILE = HW_DIR / ".active_bugs.json"


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Bug manifest not found at {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text())


def get_active_bugs() -> list[str]:
    if ACTIVE_BUGS_FILE.exists():
        try:
            return json.loads(ACTIVE_BUGS_FILE.read_text()).get("active_bugs", [])
        except Exception:
            return []
    return []


def _set_active_bugs(bug_ids: list[str]) -> None:
    ACTIVE_BUGS_FILE.write_text(json.dumps({"active_bugs": bug_ids}, indent=2))


def restore_golden() -> bool:
    """Restores picorv32_mutable.sv bit-for-bit from the pristine picorv32_golden.sv."""
    if not GOLDEN_RTL.exists():
        raise FileNotFoundError(f"Golden RTL reference not found at {GOLDEN_RTL}")
    shutil.copy2(GOLDEN_RTL, MUTABLE_RTL)
    _set_active_bugs([])
    return True


def inject_bug(bug_id: str) -> bool:
    """Inject a single bug into picorv32_mutable.sv from the manifest."""
    return inject_multiple([bug_id])


def inject_multiple(bug_ids: list[str]) -> bool:
    """Inject one or more bugs concurrently (for Experiment E2 multi-bug)."""
    manifest = load_manifest()
    for bid in bug_ids:
        if bid not in manifest:
            raise KeyError(f"Unknown bug_id: '{bid}'. Available: {list(manifest.keys())}")

    # Always start fresh from the golden baseline
    restore_golden()
    content = MUTABLE_RTL.read_text()

    injected = []
    for bid in bug_ids:
        entry = manifest[bid]
        marker = entry["line_marker"]
        replacement = entry["replacement"]
        if marker not in content:
            print(f"[bug_injector] ERROR: Marker for bug '{bid}' not found in {MUTABLE_RTL.name}: {marker}", file=sys.stderr)
            restore_golden()
            return False
        content = content.replace(marker, replacement, 1)
        injected.append(bid)

    MUTABLE_RTL.write_text(content)
    _set_active_bugs(injected)
    return True


def list_bugs() -> dict:
    manifest = load_manifest()
    active = set(get_active_bugs())
    for bid, data in manifest.items():
        data["active"] = bid in active
    return manifest


def main():
    parser = argparse.ArgumentParser(description="PicoRV32 Bug Injection Framework")
    parser.add_argument("--list", action="store_true", help="List all bugs in the taxonomy")
    parser.add_argument("--status", action="store_true", help="Check currently active bugs")
    parser.add_argument("--inject", type=str, nargs="+", help="Inject one or more bug IDs")
    parser.add_argument("--restore", action="store_true", help="Restore pristine golden RTL")

    args = parser.parse_args()

    if args.list:
        bugs = list_bugs()
        print(f"\nAvailable Bug Taxonomy ({len(bugs)} bugs):")
        print("=" * 70)
        for bid, b in bugs.items():
            status = "[ACTIVE]" if b.get("active") else "[INACTIVE]"
            print(f"- {bid} ({b['level']} - {b['subsystem']}) {status}")
            print(f"  Name:        {b['name']}")
            print(f"  Description: {b['description']}")
            print(f"  Symptom:     {b['expected_symptom']}\n")
        return

    if args.status:
        active = get_active_bugs()
        if active:
            print(f"Currently active bugs: {', '.join(active)}")
        else:
            print("System is in PRISTINE GOLDEN state (no active bugs).")
        return

    if args.restore:
        restore_golden()
        print("Successfully restored picorv32_mutable.sv to PRISTINE GOLDEN state.")
        return

    if args.inject:
        ok = inject_multiple(args.inject)
        if ok:
            print(f"Successfully injected bug(s): {', '.join(args.inject)}")
        else:
            print(f"Failed to inject bugs: {args.inject}", file=sys.stderr)
            sys.exit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
