`timescale 1 ns / 1 ps
// amoswap.w for PicoRV32 through its co-processor interface (PCPI), so the core RTL stays unmodified
// (docs/DUAL_HART.md). PicoRV32 hands undecoded instructions to PCPI; this unit claims AMOSWAP.W
// (opcode 0101111, funct3 010, funct5 00001), holds the core with pcpi_wait, and performs on its own bus port
//   old = mem[rs1]   (read, with lock: the arbiter keeps the bus for this unit's next transaction)
//   mem[rs1] = rs2   (write, lock released)
// and returns old as rd. Every other instruction is ignored, so PicoRV32 traps on it as before.
module amo_pcpi (
	input             clk,
	input             resetn,
	// PCPI (from / to the core)
	input             pcpi_valid,
	/* verilator lint_off UNUSEDSIGNAL */   // only the opcode fields of insn and the word address of rs1 are used
	input      [31:0] pcpi_insn,
	input      [31:0] pcpi_rs1,
	/* verilator lint_on UNUSEDSIGNAL */
	input      [31:0] pcpi_rs2,
	output reg        pcpi_wr,
	output reg [31:0] pcpi_rd,
	output reg        pcpi_wait,
	output reg        pcpi_ready,
	// bus port (to the arbiter)
	output reg        bus_valid,
	output reg        bus_lock,
	output reg [31:0] bus_addr,
	output reg [31:0] bus_wdata,
	output reg [3:0]  bus_wstrb,
	input             bus_ready,
	input      [31:0] bus_rdata
);
	wire is_amoswap_w = pcpi_insn[6:0] == 7'b0101111 && pcpi_insn[14:12] == 3'b010 && pcpi_insn[31:27] == 5'b00001;

	localparam IDLE = 3'd0, READ = 3'd1, WRITE = 3'd2, DONE = 3'd3, WAIT_CLR = 3'd4;
	reg [2:0]  state;
	reg [31:0] old;

	always @(posedge clk) begin
		pcpi_wr    <= 0;
		pcpi_ready <= 0;
		if (!resetn) begin
			state <= IDLE; pcpi_wait <= 0; pcpi_rd <= 0; old <= 0;
			bus_valid <= 0; bus_lock <= 0; bus_addr <= 0; bus_wdata <= 0; bus_wstrb <= 0;
		end else case (state)
			IDLE: if (pcpi_valid && is_amoswap_w) begin
				pcpi_wait <= 1;
				bus_valid <= 1;
				bus_lock  <= 1;                    // keep the bus for the write that follows
				bus_addr  <= {pcpi_rs1[31:2], 2'b00};
				bus_wstrb <= 4'b0000;
				state     <= READ;
			end
			READ: if (bus_ready) begin
				old       <= bus_rdata;
				bus_valid <= 0;
				state     <= WRITE;
			end
			WRITE: if (!bus_valid) begin
				bus_valid <= 1;                    // second transaction: the arbiter is still locked to us
				bus_lock  <= 0;
				bus_wdata <= pcpi_rs2;
				bus_wstrb <= 4'b1111;
			end else if (bus_ready) begin
				bus_valid <= 0;
				bus_wstrb <= 0;
				state     <= DONE;
			end
			DONE: begin
				pcpi_wait  <= 0;
				pcpi_wr    <= 1;
				pcpi_ready <= 1;
				pcpi_rd    <= old;
				state      <= WAIT_CLR;
			end
			default: if (!pcpi_valid) state <= IDLE;   // WAIT_CLR: the core drops pcpi_valid after pcpi_ready
		endcase
	end
endmodule
