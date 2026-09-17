// First-word-fall-through FIFO: depth 4, width 8, valid/ready handshake on both sides.
module fifo_fwft_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       in_valid_i,
    output logic       in_ready_o,
    input  logic [7:0] in_data_i,
    output logic       out_valid_o,
    input  logic       out_ready_i,
    output logic [7:0] out_data_o
);
    logic [7:0] mem [4];
    logic [1:0] wr_q, rd_q;
    logic [2:0] count_q;

    assign in_ready_o  = count_q != 3'd4;
    assign out_valid_o = count_q != 3'd0;
    assign out_data_o  = mem[rd_q];

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0;
        end else begin
            if (in_valid_i && in_ready_o) begin mem[wr_q] <= in_data_i; wr_q <= wr_q + 2'd1; end
            if (out_valid_o && out_ready_i) rd_q <= rd_q + 2'd1;
            count_q <= count_q + 3'(in_valid_i && in_ready_o) - 3'(out_valid_o && out_ready_i);
        end
    end
endmodule
