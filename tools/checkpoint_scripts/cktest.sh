cd /repo
bash rocket/build_dut.sh /root/ext/encarsia/out/rocket/driver/1081/reference_driver.rtlil /root/g >/dev/null 2>&1 || tail -3 /root/g/build.log
python3 /s/cktest.py
