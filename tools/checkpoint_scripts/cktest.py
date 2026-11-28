import os, sys
from pathlib import Path
sys.path.insert(0, "/repo/rocket")
import rv64fuzz as f
sim = Path("/root/g/Vtop_tiny_soc")
for k in ("4", "1"):
    os.environ["BENCHFUZZ_CHECKPOINT"] = k
    for prof in ("base", "ext"):
        r = f.campaign(sim, 60, 777, 40, 400000, True, False, profile=prof, core="rocket")
        print(f"K={k} {prof}: programs {r['programs']} mismatches {r['mismatches']} hangs {r['nonterminating']} spike_fail {r['spike_failures']} s {r['seconds']}", flush=True)
