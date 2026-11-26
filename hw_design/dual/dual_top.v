`timescale 1 ns / 1 ps
// Dual-hart PicoRV32 system (docs/DUAL_HART.md). Build with -DRISCV_FORMAL so both cores expose RVFI.
// Two unmodified picorv32 cores (hart 0 resets to 0x0000, hart 1 to 0x4000), one amo_pcpi unit per hart, and one
// round-robin arbiter over four requesters (0: hart 0, 1: hart 1, 2: AMO 0, 3: AMO 1) in front of a single memory
// port that the testbench services. The arbiter's grant log is the total order of every memory transaction.
module dual_top #(
	parameter [31:0] PROGADDR_HART0 = 32'h0000_0000,
	parameter [31:0] PROGADDR_HART1 = 32'h0000_4000
) (
	input             clk,
	input             resetn,
	output            trap0,
	output            trap1,
	// shared memory port
	output            mem_valid,
	output            mem_instr,
	output     [31:0] mem_addr,
	output     [31:0] mem_wdata,
	output     [3:0]  mem_wstrb,
	input             mem_ready,
	input      [31:0] mem_rdata,
	// grant log
	output            log_valid,
	output     [1:0]  log_id,
	output            log_instr,
	output     [31:0] log_addr,
	output     [3:0]  log_wstrb,
	output     [31:0] log_data,
	// replay
	input             force_valid,
	input      [1:0]  force_id,
	// RVFI of both harts (the fields the scoreboard uses), index 0 / 1
	output     [1:0]  rvfi_valid,
	output     [63:0] rvfi_insn,
	output     [1:0]  rvfi_trap,
	output     [63:0] rvfi_pc_rdata,
	output     [63:0] rvfi_pc_wdata,
	output     [9:0]  rvfi_rd_addr,
	output     [63:0] rvfi_rd_wdata,
	output     [63:0] rvfi_mem_addr,
	output     [7:0]  rvfi_mem_rmask,
	output     [7:0]  rvfi_mem_wmask,
	output     [63:0] rvfi_mem_rdata,
	output     [63:0] rvfi_mem_wdata
);
	// requester buses, index: 0 hart0, 1 hart1, 2 amo0, 3 amo1
	wire [3:0]   r_valid, r_instr, r_lock, r_ready;
	wire [127:0] r_addr, r_wdata;
	wire [15:0]  r_wstrb;
	wire [31:0]  r_rdata;
	wire [1:0]   traps;
	assign trap0 = traps[0];
	assign trap1 = traps[1];

	assign r_lock[1:0]  = 2'b00;
	assign r_instr[3:2] = 2'b00;

	genvar h;
	generate for (h = 0; h < 2; h = h + 1) begin : hart
		wire        pcpi_valid, pcpi_wr, pcpi_wait, pcpi_ready;
		wire [31:0] pcpi_insn, pcpi_rs1, pcpi_rs2, pcpi_rd;
		/* verilator lint_off PINCONNECTEMPTY */
		picorv32 #(
			.ENABLE_PCPI(1), .ENABLE_FAST_MUL(1), .ENABLE_DIV(1), .REGS_INIT_ZERO(1),
			.PROGADDR_RESET(h == 0 ? PROGADDR_HART0 : PROGADDR_HART1)
		) core (
			.clk(clk), .resetn(resetn), .trap(traps[h]),
			.mem_valid(r_valid[h]), .mem_instr(r_instr[h]), .mem_ready(r_ready[h]),
			.mem_addr(r_addr[h*32 +: 32]), .mem_wdata(r_wdata[h*32 +: 32]), .mem_wstrb(r_wstrb[h*4 +: 4]),
			.mem_rdata(r_rdata),
			.mem_la_read(), .mem_la_write(), .mem_la_addr(), .mem_la_wdata(), .mem_la_wstrb(),
			.pcpi_valid(pcpi_valid), .pcpi_insn(pcpi_insn), .pcpi_rs1(pcpi_rs1), .pcpi_rs2(pcpi_rs2),
			.pcpi_wr(pcpi_wr), .pcpi_rd(pcpi_rd), .pcpi_wait(pcpi_wait), .pcpi_ready(pcpi_ready),
			.irq(32'b0), .eoi(),
			.rvfi_valid(rvfi_valid[h]), .rvfi_order(), .rvfi_insn(rvfi_insn[h*32 +: 32]),
			.rvfi_trap(rvfi_trap[h]), .rvfi_halt(), .rvfi_intr(), .rvfi_mode(), .rvfi_ixl(),
			.rvfi_rs1_addr(), .rvfi_rs2_addr(), .rvfi_rs1_rdata(), .rvfi_rs2_rdata(),
			.rvfi_rd_addr(rvfi_rd_addr[h*5 +: 5]), .rvfi_rd_wdata(rvfi_rd_wdata[h*32 +: 32]),
			.rvfi_pc_rdata(rvfi_pc_rdata[h*32 +: 32]), .rvfi_pc_wdata(rvfi_pc_wdata[h*32 +: 32]),
			.rvfi_mem_addr(rvfi_mem_addr[h*32 +: 32]), .rvfi_mem_rmask(rvfi_mem_rmask[h*4 +: 4]),
			.rvfi_mem_wmask(rvfi_mem_wmask[h*4 +: 4]), .rvfi_mem_rdata(rvfi_mem_rdata[h*32 +: 32]),
			.rvfi_mem_wdata(rvfi_mem_wdata[h*32 +: 32]),
			.rvfi_csr_mcycle_rmask(), .rvfi_csr_mcycle_wmask(), .rvfi_csr_mcycle_rdata(), .rvfi_csr_mcycle_wdata(),
			.rvfi_csr_minstret_rmask(), .rvfi_csr_minstret_wmask(), .rvfi_csr_minstret_rdata(),
			.rvfi_csr_minstret_wdata(),
			.trace_valid(), .trace_data()
		);
		/* verilator lint_on PINCONNECTEMPTY */
		amo_pcpi amo (
			.clk(clk), .resetn(resetn),
			.pcpi_valid(pcpi_valid), .pcpi_insn(pcpi_insn), .pcpi_rs1(pcpi_rs1), .pcpi_rs2(pcpi_rs2),
			.pcpi_wr(pcpi_wr), .pcpi_rd(pcpi_rd), .pcpi_wait(pcpi_wait), .pcpi_ready(pcpi_ready),
			.bus_valid(r_valid[2+h]), .bus_lock(r_lock[2+h]), .bus_addr(r_addr[(2+h)*32 +: 32]),
			.bus_wdata(r_wdata[(2+h)*32 +: 32]), .bus_wstrb(r_wstrb[(2+h)*4 +: 4]),
			.bus_ready(r_ready[2+h]), .bus_rdata(r_rdata)
		);
	end endgenerate

	arbiter_rr #(.N(4), .IDW(2)) arb (
		.clk(clk), .resetn(resetn),
		.req_valid(r_valid), .req_instr(r_instr), .req_lock(r_lock), .req_addr(r_addr), .req_wdata(r_wdata),
		.req_wstrb(r_wstrb), .force_valid(force_valid), .force_id(force_id),
		.req_ready(r_ready), .req_rdata(r_rdata),
		.mem_valid(mem_valid), .mem_instr(mem_instr), .mem_addr(mem_addr), .mem_wdata(mem_wdata),
		.mem_wstrb(mem_wstrb), .mem_ready(mem_ready), .mem_rdata(mem_rdata),
		.log_valid(log_valid), .log_id(log_id), .log_instr(log_instr), .log_addr(log_addr), .log_wstrb(log_wstrb), .log_data(log_data)
	);
endmodule
