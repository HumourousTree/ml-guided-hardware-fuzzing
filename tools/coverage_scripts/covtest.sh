#!/bin/bash
# Feasibility of Verilator coverage on the bug-free Rocket: build plain / line / toggle, run 6 programs on each.
set -u
cd /repo
REF=/root/ext/encarsia/out/rocket/driver/$(ls /root/ext/encarsia/out/rocket/driver | head -1)/reference_driver.rtlil
echo "ref $REF"
OUT=/s/covtest; mkdir -p $OUT
for m in plain line toggle; do
  t0=$(date +%s)
  if [ $m = plain ]; then COVERAGE= bash rocket/build_dut.sh $REF /root/b_$m >/dev/null 2>&1
  else COVERAGE=$m bash rocket/build_dut.sh $REF /root/b_$m >/dev/null 2>&1; fi
  echo "build $m: $(( $(date +%s) - t0 )) s, exists: $(ls /root/b_$m/Vtop_tiny_soc 2>/dev/null)"
  [ -x /root/b_$m/Vtop_tiny_soc ] || tail -20 /root/b_$m/build.log
done
python3 - <<'EOF'
import random, time, os, re, subprocess, sys, tempfile, collections
from pathlib import Path
sys.path.insert(0, "/repo/rocket")
import rv64fuzz as f
work = Path(tempfile.mkdtemp())
progs = []
for seed in range(6):
    rng = random.Random(seed)
    prof = ["base", "ext"][seed % 2]
    asm = f.gen_program(rng, 40, True, prof)
    d = work / str(seed); d.mkdir()
    progs.append(f.build_elf(asm, d))

def cov(path):
    pts = {}
    for l in open(path, errors="replace"):
        m = re.match(r"C '(.*)' (\d+)$", l.strip())
        if m:
            pts[m.group(1)] = int(m.group(2))
    return pts

for m in ("plain", "line", "toggle"):
    sim = Path(f"/root/b_{m}/Vtop_tiny_soc")
    if not sim.exists():
        print(m, "missing"); continue
    hits = []
    for i, elf in enumerate(progs):
        cf = f"/root/cov_{m}_{i}.dat"
        env = dict(f.ENV, SIMSRAMELF=str(elf), SIMLEN="20000", COVERAGEFILE=cf)
        t = time.time(); p = subprocess.run([str(sim)], env=env, capture_output=True, text=True, timeout=600); dt = time.time() - t
        stop = "Found a stop request" in p.stdout
        if m == "plain":
            print(f"{m} prog {i}: {dt:.2f}s stop={stop}"); continue
        c = cov(cf); h = {k for k, v in c.items() if v > 0}; hits.append(h)
        print(f"{m} prog {i}: {dt:.2f}s stop={stop} points {len(c)} hit {len(h)} file {os.path.getsize(cf)//1024} KB")
    if hits:
        u = set().union(*hits); inter = set.intersection(*hits)
        print(f"{m}: union {len(u)}, intersection {len(inter)}, per-program new over first: {[len(h - hits[0]) for h in hits]}")
        # by module (hier field 'h' in Verilator's key)
        mods = collections.Counter()
        for k in u - inter:
            mm = re.search(r"\x01h\x02([^\x01]*)", k)
            mods[(mm.group(1).split(".")[2:4] and ".".join(mm.group(1).split(".")[2:4])) if mm else "?"] += 1
        print("  varying points by hierarchy:", mods.most_common(12))
EOF
