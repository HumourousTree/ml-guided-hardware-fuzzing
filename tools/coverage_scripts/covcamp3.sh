#!/bin/bash
# Coverage feedback end to end on Rocket driver bug 1081: line-coverage golden + buggy DUT; mutate_p 0.7 vs 0.
set -u
cd /repo
B=/root/ext/encarsia/out/rocket/driver/1081
COVERAGE=toggle COVERAGE_MAX_WIDTH=1 bash rocket/build_dut.sh $B/reference_driver.rtlil /root/g_line > /dev/null 2>&1 || tail /root/g_line/build.log
bash rocket/build_dut.sh $B/reference_driver.rtlil /root/g_plain > /dev/null 2>&1 || tail /root/g_plain/build.log
bash rocket/build_dut.sh $B/host_driver.rtlil /root/bug > /dev/null 2>&1 || tail /root/bug/build.log
ls -la /root/g_line/Vtop_tiny_soc /root/bug/Vtop_tiny_soc
OUT=/s/covcamp3; rm -rf $OUT; mkdir -p $OUT
N=${N:-300}
for prof in base ext; do
  for p in 0.7 0.0; do
    for seed in 0 1; do
      python3 rocket/rv64fuzz.py --sim /root/bug/Vtop_tiny_soc --cov-sim /root/g_line/Vtop_tiny_soc --ref-sim /root/g_plain/Vtop_tiny_soc --mutate-p $p \
        --programs $N --seed $seed --profile $prof --out $OUT/${prof}_p${p}_s${seed}.json > $OUT/${prof}_p${p}_s${seed}.log 2>&1 &
    done
  done
  wait
done
python3 - <<'EOF'
import json, glob
for f in sorted(glob.glob("/s/covcamp3/*.json")):
    r = json.load(open(f))
    fd = r.get("first_detection") or {}
    print(f.split("/")[-1], "progs", r["programs"], "cov", r["coverage_points"], "corpus", r["corpus"], "mut", r["mutated"],
          "asmfail", r["assembly_failures"], "spikefail", r["spike_failures"], "refdis", r.get("reference_disagreements", 0),
          "mism", r["mismatches"], "hang", r["nonterminating"], "first", fd.get("n_programs"), fd.get("mutated"),
          "s", r["seconds"], "curve", r["coverage_curve"][::3])
EOF
