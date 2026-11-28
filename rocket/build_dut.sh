#!/usr/bin/env bash
# Build one Rocket or BOOM SoC simulator (Cascade's tiny SoC + one Encarsia host netlist) with Verilator.
#   [CORE=rocket|boom] bash rocket/build_dut.sh <host.rtlil> <out_dir>
# The DUT is exactly what Encarsia fuzzes: cascade_receptor.v (Rocket SoC without the Rocket core module) + the host
# module exported from the given RTLIL (golden: the bug's reference_*.rtlil or multiplexer/reference.rtlil).
set -euo pipefail
HERE=$(dirname "$(realpath "$0")")
RTLIL=$(realpath "$1"); OUT=$(realpath -m "$2"); JOBS=${JOBS:-1}
# Memory safety: the SoC is ~10 MB of Verilog; parallel g++ at -O1 exhausted the WSL VM (15 GB) and restarted it, killing
# every other job.  One compile job, -O0 (Cascade's own setting), split output files, and a per-process cap so a runaway
# compile fails here instead of taking the VM down.
# SIM_OPT: C++ optimisation of the simulator, default 1 (validated 2026-11-28 against -O0 with 150-run campaigns whose
# counters, first detecting programs and calibrations are all identical: Rocket 2.9x faster per program, runs 46122923685
# vs 46119438532; BOOM 3.2x, runs 46124661434 vs 46124658586). -O2 miscompiles Rocket (wrong register dumps on every
# program; results/v0.9/speed/).
ulimit -v $(( ${MEM_CAP_GB:-10} * 1024 * 1024 ))
EXT=${EXT:-$HOME/ext}
CORE=${CORE:-rocket}
SIM_OPT=${SIM_OPT:-1}
# COVERAGE=line|toggle|line,toggle: Verilator coverage of the whole SoC; Cascade's toplevel.cc writes it to $COVERAGEFILE
# at the end of each run (HAS_COVERAGE). Used for coverage feedback on the bug-free core only; detection builds stay plain.
COVFLAGS=(); COVDEF=""
COVERAGE=${COVERAGE:-}
for c in ${COVERAGE//,/ }; do COVFLAGS+=("--coverage-$c"); COVDEF="-DHAS_COVERAGE"; done
# COVERAGE_MAX_WIDTH=n: toggle coverage only of signals at most n bits wide (1: single-bit, mostly control signals)
[ -n "${COVERAGE_MAX_WIDTH:-}" ] && COVFLAGS+=("--coverage-max-width" "$COVERAGE_MAX_WIDTH")
CORPUS=$EXT/encarsia/out/$CORE; TB=$EXT/cascade-chipyard/cascade-$CORE; CM=$EXT/cascade-meta-common
mkdir -p "$OUT" && cd "$OUT"
[ -x Vtop_tiny_soc ] && { echo "already built: $OUT/Vtop_tiny_soc"; exit 0; }
yosys -q -p "read_rtlil $RTLIL; write_verilog -noattr host.v" > yosys.log 2>&1
python3 "$HERE/merge_dut.py" "$CORPUS/cascade_receptor.v" host.v dut.v | tee merge.log
verilator --cc --exe --build -j "$JOBS" -O2 --output-split 20000 --output-split-cfuncs 2000 --top-module top_tiny_soc -Mdir obj \
  "${COVFLAGS[@]}" \
  -CFLAGS "-std=c++17 $COVDEF -DIS_VANILLA -DSYNTHESIS -DTOPLEVEL_NAME=top_tiny_soc -O${SIM_OPT:-0} -I$CM/dv -I$TB/dv" \
  -DSTOP_COND=0 -LDFLAGS "-pthread -lutil" \
  -Wno-fatal -Wno-lint -Wno-style -Wno-PINCONNECTEMPTY -Wno-BLKANDNBLK -Wno-REDEFMACRO -Wno-PINMISSING -Wno-DECLFILENAME \
  -Wno-TIMESCALEMOD -Wno-VARHIDDEN -Wno-EOFNEWLINE -Wno-MULTIDRIVEN -Wno-UNOPTFLAT -Wno-CASEINCOMPLETE \
  dut.v "$HERE/sram_mem.sv" "$TB/src/top_tiny_soc.sv" \
  "$CM/dv/common_functions.cc" "$CM/dv/elfloader.cc" "$TB/dv/toplevel.cc" -I"$TB/dv" -I"$CM/dv" > build.log 2>&1
cp obj/Vtop_tiny_soc . && rm -rf obj && echo "built: $OUT/Vtop_tiny_soc"
