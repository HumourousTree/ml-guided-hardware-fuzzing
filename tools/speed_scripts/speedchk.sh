cd /repo
REF=/root/ext/encarsia/out/rocket/driver/1081/reference_driver.rtlil
for o in 0 1 2; do SIM_OPT=$o bash rocket/build_dut.sh $REF /root/opt$o >/dev/null 2>&1; done
python3 /s/speedchk.py
