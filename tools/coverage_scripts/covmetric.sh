#!/bin/bash
# Which Verilator coverage metric keeps growing? expr / toggle (1-bit) / line on the bug-free Rocket: build time,
# run time, points, and the union over 80 random programs (base+ext) — saturation curve.
set -u
cd /repo
REF=/root/ext/encarsia/out/rocket/driver/1081/reference_driver.rtlil
for m in line expr toggle1; do
  t0=$(date +%s)
  case $m in
    line) COVERAGE=line bash rocket/build_dut.sh $REF /root/c_$m >/dev/null 2>&1 ;;
    expr) COVERAGE=expr bash rocket/build_dut.sh $REF /root/c_$m >/dev/null 2>&1 ;;
    toggle1) COVERAGE=toggle COVERAGE_MAX_WIDTH=1 bash rocket/build_dut.sh $REF /root/c_$m >/dev/null 2>&1 ;;
  esac
  echo "build $m: $(( $(date +%s) - t0 )) s $(ls /root/c_$m/Vtop_tiny_soc 2>/dev/null)"
  [ -x /root/c_$m/Vtop_tiny_soc ] || tail -5 /root/c_$m/build.log
done
python3 - <<'EOF'
import random, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "/repo/rocket")
import rv64fuzz as f
w = Path(tempfile.mkdtemp())
elfs = []
for seed in range(80):
    d = w / str(seed); d.mkdir()
    elfs.append(f.build_elf(f.gen_program(random.Random(1000 + seed), 40, True, ["base", "ext"][seed % 2]), d))
for m in ("line", "expr", "toggle1"):
    sim = Path(f"/root/c_{m}/Vtop_tiny_soc")
    if not sim.exists():
        print(m, "not built"); continue
    u, curve, ts, total = set(), [], [], 0
    first = None
    for i, elf in enumerate(elfs):
        cf = w / "c.dat"; cf.unlink(missing_ok=True)
        t = time.time(); f.run_rocket(sim, elf, 400000, cov_file=cf); ts.append(time.time() - t)
        pts = f.read_coverage(cf)
        if first is None:
            first = pts
            total = sum(1 for l in open(cf, errors="replace") if l.startswith("C '"))
        u |= pts
        if (i + 1) in (1, 2, 5, 10, 20, 40, 60, 80):
            curve.append((i + 1, len(u)))
    print(f"{m}: points {total}, run {sum(ts)/len(ts):.2f}s, union curve {curve}, growth 40->80: {curve[-1][1] - curve[-3][1]}")
EOF
