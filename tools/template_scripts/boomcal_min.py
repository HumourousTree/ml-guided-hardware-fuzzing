"""Shrink a calibration-failing program: the failure is 'bug-free BOOM disagrees with Spike'. Prints the minimal body."""
import json, sys, tempfile
from pathlib import Path
REPO = Path("/mnt/c/Users/subod/Downloads/ML Guided Hardware Fuzzing_Parent/ML Guided Hardware fuzzing")
sys.path.insert(0, str(REPO / "rocket")); sys.path.insert(0, str(REPO))
import rv64fuzz
from harness.delta_debugger import _ddmin_units

r = json.load(open(sys.argv[1]))
asm = r["first_detection"]["asm"]
lines = asm.split("\n")
# body = from first line after the FP/prologue setup up to the epilogue dump pointer load
start = max(i for i, l in enumerate(lines) if l.strip().startswith("fsrmi") or l.strip().startswith("fmv.d.x")) + 1
end = next(i for i in range(start, len(lines)) if lines[i].strip() == f"li x31, {rv64fuzz.DUMP}")
body = lines[start:end]
sim = Path.home() / "boom_runs" / "golden_multiplexer" / "Vtop_tiny_soc"
w = Path(tempfile.mkdtemp())

def fails(b):
    try:
        elf = rv64fuzz.build_elf("\n".join(lines[:start] + b + lines[end:]), w)
    except Exception:
        return False
    exp = rv64fuzz.run_spike(elf)
    if exp is None:
        return False
    got, stop = rv64fuzz.run_rocket(sim, elf, 400000)
    return stop and got != exp

print("original fails:", fails(body), len(body), flush=True)
units = [[l] for l in body]
kept, t = _ddmin_units(units, fails, 400)
print("tests", t)
print("\n".join(l for u in kept for l in u))
