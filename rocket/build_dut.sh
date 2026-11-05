#!/usr/bin/env bash
# Build one Rocket SoC simulator (Cascade's tiny SoC + one Encarsia host netlist) with Verilator.
#   bash rocket/build_dut.sh <host.rtlil> <out_dir>
# The DUT is exactly what Encarsia fuzzes: cascade_receptor.v (Rocket SoC without the Rocket core module) + the host
# module exported from the given RTLIL (golden: the bug's reference_*.rtlil or multiplexer/reference.rtlil).
set -euo pipefail
HERE=$(dirname "$(realpath "$0")")
RTLIL=$(realpath "$1"); OUT=$(realpath -m "$2"); JOBS=${JOBS:-1}
# Memory safety: the SoC is ~10 MB of Verilog; parallel g++ at -O1 exhausted the WSL VM (15 GB) and restarted it, killing
# every other job.  One compile job, -O0 (Cascade's own setting), split output files, and a per-process cap so a runaway
# compile fails here instead of taking the VM down.
ulimit -v $(( ${MEM_CAP_GB:-10} * 1024 * 1024 ))
EXT=${EXT:-$HOME/ext}
ROCKET=$EXT/encarsia/out/rocket; TB=$EXT/cascade-chipyard/cascade-rocket; CM=$EXT/cascade-meta-common
mkdir -p "$OUT" && cd "$OUT"
[ -x Vtop_tiny_soc ] && { echo "already built: $OUT/Vtop_tiny_soc"; exit 0; }
yosys -q -p "read_rtlil $RTLIL; write_verilog -noattr host.v" > yosys.log 2>&1
python3 "$HERE/merge_dut.py" "$ROCKET/cascade_receptor.v" host.v dut.v | tee merge.log
verilator --cc --exe --build -j "$JOBS" -O2 --output-split 20000 --output-split-cfuncs 2000 --top-module top_tiny_soc -Mdir obj \
  -CFLAGS "-std=c++17 -DIS_VANILLA -DSYNTHESIS -DTOPLEVEL_NAME=top_tiny_soc -O0 -I$CM/dv -I$TB/dv" \
  -DSTOP_COND=0 -LDFLAGS "-pthread -lutil" \
  -Wno-fatal -Wno-lint -Wno-style -Wno-PINCONNECTEMPTY -Wno-BLKANDNBLK -Wno-REDEFMACRO -Wno-PINMISSING -Wno-DECLFILENAME \
  -Wno-TIMESCALEMOD -Wno-VARHIDDEN -Wno-EOFNEWLINE -Wno-MULTIDRIVEN -Wno-UNOPTFLAT -Wno-CASEINCOMPLETE \
  dut.v "$HERE/sram_mem.sv" "$TB/src/top_tiny_soc.sv" \
  "$CM/dv/common_functions.cc" "$CM/dv/elfloader.cc" "$TB/dv/toplevel.cc" -I"$TB/dv" -I"$CM/dv" > build.log 2>&1
cp obj/Vtop_tiny_soc . && rm -rf obj && echo "built: $OUT/Vtop_tiny_soc"
