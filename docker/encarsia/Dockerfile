# Toolchain image for the Rocket/BOOM (Encarsia) campaigns on cloud runners.
# Same versions as the reference WSL setup (scripts/setup_rocket_env.sh): Ubuntu 26.04, Verilator 5.032, Yosys 0.52,
# Spike at commit 1b9f9c8, EnCorpus (Encarsia b8fd17d), Cascade's tiny SoCs (cascade-chipyard 727a99a) and common
# testbench files (encarsia-cascade 2e90bec). The repository itself is NOT baked in: jobs check it out and mount it.
#   docker build -t encarsia-env docker/encarsia
FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      verilator yosys binutils-riscv64-unknown-elf device-tree-compiler libfdt-dev \
      build-essential git ca-certificates python3 make g++ time \
    && rm -rf /var/lib/apt/lists/*

# Spike (riscv-isa-sim), pinned: v1.1.0 does not build with GCC 15
ARG SPIKE_COMMIT=1b9f9c8
RUN git clone https://github.com/riscv-software-src/riscv-isa-sim /tmp/spike && cd /tmp/spike \
    && git checkout ${SPIKE_COMMIT} && git submodule update --init --depth 1 \
    && mkdir build && cd build && ../configure --prefix=/usr/local >/dev/null \
    && make -j"$(nproc)" >/dev/null && make install >/dev/null && cd / && rm -rf /tmp/spike

# Bug corpora and Cascade's harness, pinned
ENV EXT=/root/ext
RUN mkdir -p $EXT && cd $EXT \
    && git clone https://github.com/comsec-group/encarsia.git && cd encarsia && git checkout b8fd17d \
    && mkdir -p out && tar xzf EnCorpus_rocket.tar.gz -C out && tar xzf EnCorpus_boom.tar.gz -C out \
    && rm -f EnCorpus_*.tar.gz && cd $EXT \
    && git clone --filter=blob:none --sparse https://github.com/cascade-artifacts-designs/cascade-chipyard \
    && cd cascade-chipyard && git checkout 727a99a && git sparse-checkout set cascade-rocket cascade-boom cascade-common && cd $EXT \
    && git clone --filter=blob:none https://github.com/encarsia-artifacts/encarsia-cascade /tmp/ec \
    && cd /tmp/ec && git checkout 2e90bec \
    && mkdir -p $EXT/cascade-meta-common && cp -r design-processing/common/dv design-processing/common/src $EXT/cascade-meta-common/ \
    && echo "encarsia-artifacts/encarsia-cascade 2e90bec" > $EXT/cascade-meta-common/COMMIT && rm -rf /tmp/ec

# Encarsia's Yosys fork (bug injection passes inject / inject_driver / inject_map), pinned, installed under
# /opt/encarsia-yosys so that the stock Yosys 0.52 used by every other flow stays the default `yosys`.
# Its bundled json11 predates GCC's stricter headers, hence "-include cstdint" (source unchanged).
ARG ENCARSIA_YOSYS_COMMIT=24b50664c2
RUN apt-get update && apt-get install -y --no-install-recommends \
      bison flex libreadline-dev gawk tcl-dev libffi-dev pkg-config zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && git clone https://github.com/encarsia-artifacts/encarsia-yosys.git /tmp/eyosys && cd /tmp/eyosys \
    && git checkout ${ENCARSIA_YOSYS_COMMIT} && git submodule update --init --recursive 2>/dev/null || true \
    && cd /tmp/eyosys && make config-gcc >/dev/null \
    && echo "CXXFLAGS += -include cstdint" >> Makefile.conf \
    && make -j"$(nproc)" PREFIX=/opt/encarsia-yosys >/dev/null \
    && make install PREFIX=/opt/encarsia-yosys >/dev/null && cd / && rm -rf /tmp/eyosys \
    && /opt/encarsia-yosys/bin/yosys -V

# Quick self-test of the tools (fails the build if anything is missing)
RUN verilator --version && yosys -V && spike --help >/dev/null 2>&1; which spike riscv64-unknown-elf-as dtc
WORKDIR /work
