#!/bin/bash
set -u
cd /repo
B=/root/ext/encarsia/out/rocket/driver/1081
bash rocket/build_dut.sh $B/reference_driver.rtlil /root/g_plain > /dev/null 2>&1
COVERAGE=line bash rocket/build_dut.sh $B/reference_driver.rtlil /root/g_line > /dev/null 2>&1
python3 - <<'EOF'
import random, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/repo/rocket")
import rv64fuzz as f
w = Path(tempfile.mkdtemp())
for seed in range(8):
    asm = f.gen_program(random.Random(seed), 40, True, "base")
    elf = f.build_elf(asm, w)
    exp = f.run_spike(elf)
    p, _ = f.run_rocket(Path("/root/g_plain/Vtop_tiny_soc"), elf, 400000)
    l, _ = f.run_rocket(Path("/root/g_line/Vtop_tiny_soc"), elf, 400000)
    print(seed, "fcsr spike %#x plain %#x line %#x" % (exp[62], p[62], l[62]))
# no FP at all
asm = f.gen_program(random.Random(3), 40, False, "base")
elf = f.build_elf(asm, w); exp = f.run_spike(elf)
l, _ = f.run_rocket(Path("/root/g_line/Vtop_tiny_soc"), elf, 400000)
print("no-fp program: line agrees", l == exp, len(l), len(exp))
EOF
