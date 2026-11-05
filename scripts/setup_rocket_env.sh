#!/usr/bin/env bash
# Step 3 environment (Rocket + Encarsia), user space only (no sudo).  Idempotent; run inside WSL:
#   bash scripts/setup_rocket_env.sh            -> ~/opt/bench3/{bin,lib}, ~/ext/{encarsia,encarsia-meta,cascade-chipyard,cascade-meta}
# Pinned versions:
#   Ubuntu packages (extracted with dpkg -x): binutils-riscv64-unknown-elf, device-tree-compiler, libfdt1
#   Spike (riscv-isa-sim) commit 1b9f9c8 (2026-10-21), built from source
#   Encarsia b8fd17d (EnCorpus), encarsia-meta 756ba14, cascade-chipyard 727a99a (cascade-rocket testbench only)
set -euo pipefail
PREFIX=${PREFIX:-$HOME/opt/bench3}; EXT=${EXT:-$HOME/ext}; JOBS=${JOBS:-6}
mkdir -p "$PREFIX" "$EXT"
log() { echo "[setup $(date '+%T')] $*"; }

# 1. Ubuntu packages, extracted into $PREFIX/deb-root (no root needed)
if [ ! -x "$PREFIX/bin/riscv64-unknown-elf-as" ] || [ ! -x "$PREFIX/bin/dtc" ]; then
  log "extracting binutils-riscv64-unknown-elf, device-tree-compiler, libfdt1"
  tmp=$(mktemp -d); (cd "$tmp" && apt-get download binutils-riscv64-unknown-elf device-tree-compiler libfdt1 >/dev/null)
  mkdir -p "$PREFIX/deb-root" "$PREFIX/bin" "$PREFIX/lib"
  for d in "$tmp"/*.deb; do dpkg -x "$d" "$PREFIX/deb-root"; done
  ln -sf "$PREFIX"/deb-root/usr/bin/* "$PREFIX/bin/"
  cp -a "$PREFIX"/deb-root/usr/lib/x86_64-linux-gnu/libfdt* "$PREFIX/lib/" 2>/dev/null || true
  rm -rf "$tmp"
fi
export PATH="$PREFIX/bin:$PATH" LD_LIBRARY_PATH="$PREFIX/lib:${LD_LIBRARY_PATH:-}"
riscv64-unknown-elf-as --version | head -1; dtc --version

# 2. Spike at a pinned commit (v1.1.0 from 2021 does not compile with GCC 15)
SPIKE_COMMIT=1b9f9c8f4bef2710b1db3196d51bea2f7be79fa3          # 2026-10-21
if [ ! -x "$PREFIX/bin/spike" ]; then
  log "building Spike $SPIKE_COMMIT"
  [ -d "$EXT/riscv-isa-sim" ] || git clone -q --depth 50 https://github.com/riscv-software-src/riscv-isa-sim.git "$EXT/riscv-isa-sim"
  (cd "$EXT/riscv-isa-sim" && git checkout -q "$SPIKE_COMMIT" && git submodule update --init --depth 1)   # softfloat
  mkdir -p "$EXT/riscv-isa-sim/build" && cd "$EXT/riscv-isa-sim/build"
  ../configure --prefix="$PREFIX" >/dev/null
  make -j"$JOBS" >/dev/null && make install >/dev/null
fi
spike --help 2>&1 | head -1

# 3. Encarsia corpus and Cascade's Rocket testbench (sources only; the Rocket SoC Verilog is in EnCorpus)
[ -d "$EXT/encarsia" ] || git clone -q --depth 1 https://github.com/comsec-group/encarsia.git "$EXT/encarsia"
[ -d "$EXT/encarsia/out/rocket" ] || (mkdir -p "$EXT/encarsia/out" && cd "$EXT/encarsia/out" && tar xzf ../EnCorpus_rocket.tar.gz)
[ -d "$EXT/encarsia-meta" ] || git clone -q --depth 1 https://github.com/encarsia-artifacts/encarsia-meta.git "$EXT/encarsia-meta"
if [ ! -d "$EXT/cascade-chipyard/cascade-rocket" ]; then
  git clone -q --depth 1 --filter=blob:none --sparse https://github.com/cascade-artifacts-designs/cascade-chipyard "$EXT/cascade-chipyard"
  (cd "$EXT/cascade-chipyard" && git sparse-checkout set cascade-rocket cascade-common)
fi
log "done: PATH=$PREFIX/bin:\$PATH LD_LIBRARY_PATH=$PREFIX/lib"
