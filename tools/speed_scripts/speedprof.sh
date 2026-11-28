#!/bin/bash
# Per-program time breakdown and two candidate speedups (Spike logging, simulator C++ optimisation) on the bug-free Rocket.
set -u
cd /repo
REF=/root/ext/encarsia/out/rocket/driver/1081/reference_driver.rtlil
for o in 0 1 2; do
  t0=$(date +%s); SIM_OPT=$o bash rocket/build_dut.sh $REF /root/opt$o >/dev/null 2>&1
  echo "build -O$o: $(( $(date +%s) - t0 )) s $(ls /root/opt$o/Vtop_tiny_soc 2>/dev/null) $(du -h /root/opt$o/Vtop_tiny_soc 2>/dev/null | cut -f1)"
  [ -x /root/opt$o/Vtop_tiny_soc ] || tail -3 /root/opt$o/build.log
done
python3 /s/speedprof.py
