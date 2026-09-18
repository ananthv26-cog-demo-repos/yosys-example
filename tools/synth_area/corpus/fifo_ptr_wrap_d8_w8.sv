// Circular FIFO without an occupancy counter: pointers carry one extra wrap bit, so
// empty = pointers equal, full = same index with different wrap bits. Depth 8, width 8.
module fifo_ptr_wrap_d8_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [7:0] mem [8];
    logic [3:0] wr_q, rd_q;   // [3] = wrap bit, [2:0] = index

    assign empty_o = wr_q == rd_q;
    assign full_o  = (wr_q[2:0] == rd_q[2:0]) && (wr_q[3] != rd_q[3]);
    assign data_o  = mem[rd_q[2:0]];

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0;
        end else begin
            if (push_i && !full_o) begin mem[wr_q[2:0]] <= data_i; wr_q <= wr_q + 4'd1; end
            if (pop_i && !empty_o) rd_q <= rd_q + 4'd1;
        end
    end
endmodule
