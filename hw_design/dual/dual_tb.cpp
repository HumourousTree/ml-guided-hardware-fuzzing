// Testbench for dual_top (docs/DUAL_HART.md). Services the single arbitrated memory port and prints one JSON object
// per line on stdout:
//   {"h":0|1, "pc_rdata":..., "pc_wdata":..., "insn":..., "rd_addr":..., "rd_wdata":..., "mem_addr":..., "mem_rmask":...,
//    "mem_wmask":..., "mem_rdata":..., "mem_wdata":..., "trap":0|1, "cycle":...}      one per retirement
//   {"g":id, "i":instr, "a":addr, "w":wstrb, "d":data, "cycle":...}                  one per granted bus transaction
//   {"halt":h, "cycle":...}                                                         hart h asserted trap (stopped)
//   {"end":"halted"|"timeout", "cycles":...}
// usage: sim_dual prog0.hex prog1.hex max_cycles mem_delay seed [replay_ids.txt]
//   prog0 is loaded at 0x0000, prog1 at 0x4000; memory is 64 KiB. mem_delay > 0 inserts 0..mem_delay random wait
//   cycles per transaction (seeded), which changes the interleaving. With a replay file (one requester id per line: the
//   "g" fields of an earlier run's data grants, i.e. those with "i":0) the arbiter grants data accesses in exactly that
//   order; fetches stay free. Entries of a hart that has halted are skipped (used while a program is being minimised).
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>
#include "Vdual_top.h"
#include "verilated.h"

static const uint32_t MEM_SIZE = 64 * 1024;
static uint8_t memory[MEM_SIZE];

static uint32_t rd32(uint32_t a) {
	a &= (MEM_SIZE - 1) & ~3u;
	return memory[a] | (memory[a + 1] << 8) | (memory[a + 2] << 16) | ((uint32_t)memory[a + 3] << 24);
}
static void wr32(uint32_t a, uint32_t d, uint32_t strb) {
	a &= (MEM_SIZE - 1) & ~3u;
	for (int i = 0; i < 4; i++)
		if (strb >> i & 1) memory[a + i] = (d >> (8 * i)) & 0xFF;
}
static void load_hex(const char* path, uint32_t base) {
	std::ifstream f(path);
	std::string line;
	uint32_t a = base;
	while (std::getline(f, line)) {
		size_t s = line.find_first_not_of(" \t\r\n");
		if (s == std::string::npos || line[s] == '#') continue;
		wr32(a, (uint32_t)std::stoul(line.substr(s), nullptr, 16), 0xF);
		a += 4;
	}
}
static uint32_t lane(uint64_t v, int h) { return (uint32_t)(v >> (32 * h)); }

int main(int argc, char** argv) {
	Verilated::commandArgs(argc, argv);
	if (argc < 6) { std::fprintf(stderr, "usage: %s prog0.hex prog1.hex max_cycles mem_delay seed [replay]\n", argv[0]); return 2; }
	std::memset(memory, 0, sizeof memory);
	load_hex(argv[1], 0x0000);
	load_hex(argv[2], 0x4000);
	long max_cycles = std::atol(argv[3]);
	int mem_delay = std::atoi(argv[4]);
	std::srand((unsigned)std::atoi(argv[5]));
	std::vector<int> replay;
	if (argc > 6) {
		std::ifstream rf(argv[6]);
		int id;
		while (rf >> id) replay.push_back(id);
	}
	size_t rp = 0;

	Vdual_top* top = new Vdual_top;
	top->clk = 0; top->resetn = 0; top->mem_ready = 0; top->mem_rdata = 0; top->force_valid = 0; top->force_id = 0;
	int wait = -1;                 // remaining wait cycles of the current transaction (-1: none in progress)
	bool halted[2] = {false, false};
	long cycle = 0;
	const char* how = "timeout";

	for (; cycle < max_cycles; cycle++) {
		top->resetn = cycle >= 10;
		while (rp < replay.size() && halted[replay[rp] & 1]) rp++;
		top->force_valid = rp < replay.size();
		top->force_id = rp < replay.size() ? replay[rp] : 0;
		top->clk = 0;
		top->eval();

		top->mem_ready = 0;
		if (top->mem_valid) {
			if (wait < 0) wait = mem_delay > 0 ? std::rand() % (mem_delay + 1) : 0;
			if (wait == 0) {
				top->mem_ready = 1;
				if (top->mem_wstrb) { wr32(top->mem_addr, top->mem_wdata, top->mem_wstrb); top->mem_rdata = 0; }
				else top->mem_rdata = rd32(top->mem_addr);
				wait = -1;
			} else wait--;
		}
		top->clk = 1;
		top->eval();
		if (!top->resetn) continue;

		if (top->log_valid) {
			std::printf("{\"g\":%u,\"i\":%u,\"a\":%u,\"w\":%u,\"d\":%u,\"cycle\":%ld}\n", top->log_id, top->log_instr,
			            top->log_addr, top->log_wstrb, top->log_data, cycle);
			if (!top->log_instr && rp < replay.size()) rp++;
		}
		for (int h = 0; h < 2; h++) {
			if (top->rvfi_valid >> h & 1)
				std::printf("{\"h\":%d,\"pc_rdata\":%u,\"pc_wdata\":%u,\"insn\":%u,\"rd_addr\":%u,\"rd_wdata\":%u,"
				            "\"mem_addr\":%u,\"mem_rmask\":%u,\"mem_wmask\":%u,\"mem_rdata\":%u,\"mem_wdata\":%u,"
				            "\"trap\":%u,\"cycle\":%ld}\n",
				            h, lane(top->rvfi_pc_rdata, h), lane(top->rvfi_pc_wdata, h), lane(top->rvfi_insn, h),
				            (top->rvfi_rd_addr >> (5 * h)) & 31, lane(top->rvfi_rd_wdata, h),
				            lane(top->rvfi_mem_addr, h), (top->rvfi_mem_rmask >> (4 * h)) & 15,
				            (top->rvfi_mem_wmask >> (4 * h)) & 15, lane(top->rvfi_mem_rdata, h),
				            lane(top->rvfi_mem_wdata, h), (top->rvfi_trap >> h) & 1, cycle);
			bool t = h ? top->trap1 : top->trap0;
			if (t && !halted[h]) { halted[h] = true; std::printf("{\"halt\":%d,\"cycle\":%ld}\n", h, cycle); }
		}
		if (halted[0] && halted[1]) { how = "halted"; cycle++; break; }
	}
	std::printf("{\"end\":\"%s\",\"cycles\":%ld}\n", how, cycle);
	top->final();
	delete top;
	return 0;
}
