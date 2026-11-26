// Round-robin arbiter for the dual-hart PicoRV32 system (docs/DUAL_HART.md).
// N requesters with PicoRV32's native memory interface share one downstream port. A grant lasts one transaction
// (until mem_ready); a requester that asserts lock keeps the grant for its next transaction too, so an AMO's read and
// write are indivisible. Every granted transaction is reported on the log port (one cycle pulse at completion), which
// is the total order of all memory accesses that the two-hart reference model replays.
module arbiter_rr #(
	parameter N = 4,
	parameter IDW = 2
) (
	input              clk,
	input              resetn,
	// requesters (flattened: requester i uses bits [i*W +: W])
	input      [N-1:0]    req_valid,
	input      [N-1:0]    req_instr,
	input      [N-1:0]    req_lock,
	input      [N*32-1:0] req_addr,
	input      [N*32-1:0] req_wdata,
	input      [N*4-1:0]  req_wstrb,
	output reg [N-1:0]    req_ready,
	output reg [31:0]     req_rdata,          // latched read data; only the requester with req_ready samples it
	// downstream memory
	output reg            mem_valid,
	output reg            mem_instr,
	output reg [31:0]     mem_addr,
	output reg [31:0]     mem_wdata,
	output reg [3:0]      mem_wstrb,
	input                 mem_ready,
	input      [31:0]     mem_rdata,
	// grant log: one pulse per completed transaction
	output reg            log_valid,
	output reg [IDW-1:0]  log_id,
	output reg [31:0]     log_addr,
	output reg [3:0]      log_wstrb,
	output reg [31:0]     log_data            // write data, or read data for loads
);
	// state: IDLE -> BUSY (downstream transaction) -> DONE (one cycle: req_ready to the requester, which drops its
	// request in the following cycle; without DONE the still-high request would be granted a second time)
	localparam IDLE = 2'd0, BUSY = 2'd1, DONE = 2'd2;
	reg  [1:0]     state;
	reg  [IDW-1:0] cur;           // requester being served
	reg  [IDW-1:0] last;          // last requester granted (round-robin pointer)
	reg            locked;        // cur holds the bus for its next transaction
	reg  [IDW-1:0] pick;
	reg            pick_valid;
	integer k;
	integer idx;

	// next requester after `last` (or the locked owner), combinational
	always @* begin
		pick = last;
		pick_valid = 0;
		idx = 0;
		if (locked) begin
			pick = cur;
			pick_valid = req_valid[cur];
		end else begin
			for (k = N; k >= 1; k = k - 1) begin        // ends on the nearest requester after `last`
				idx = ({{(32-IDW){1'b0}}, last} + k) % N;
				if (req_valid[idx[IDW-1:0]]) begin
					pick = idx[IDW-1:0];
					pick_valid = 1;
				end
			end
		end
	end

	always @(posedge clk) begin
		req_ready <= 0;
		log_valid <= 0;
		if (!resetn) begin
			state <= IDLE; cur <= 0; last <= {IDW{1'b1}}; locked <= 0; req_rdata <= 0;   // N = 2**IDW
			mem_valid <= 0; mem_instr <= 0; mem_addr <= 0; mem_wdata <= 0; mem_wstrb <= 0;
		end else case (state)
			IDLE: if (pick_valid) begin
				state     <= BUSY;
				cur       <= pick;
				mem_valid <= 1;
				mem_instr <= req_instr[pick];
				mem_addr  <= req_addr[pick*32 +: 32];
				mem_wdata <= req_wdata[pick*32 +: 32];
				mem_wstrb <= req_wstrb[pick*4 +: 4];
			end
			BUSY: if (mem_ready) begin
				state          <= DONE;
				mem_valid      <= 0;
				req_ready[cur] <= 1;
				req_rdata      <= mem_rdata;
				last           <= cur;
				locked         <= req_lock[cur];
				log_valid      <= 1;
				log_id         <= cur;
				log_addr       <= mem_addr;
				log_wstrb      <= mem_wstrb;
				log_data       <= (mem_wstrb != 0) ? mem_wdata : mem_rdata;
			end
			default: state <= IDLE;              // DONE: the requester drops its request this cycle
		endcase
	end
endmodule
