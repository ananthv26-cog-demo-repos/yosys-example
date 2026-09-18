// Circular FIFO with a combinational bypass: when empty and both sides handshake in the same
// cycle, data passes straight through without being stored. Depth 4, width 8.
module fifo_bypass_d4_w8 (
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
    logic empty, bypass, do_write, do_read;

    assign empty       = count_q == 3'd0;
    assign in_ready_o  = count_q != 3'd4;
    assign out_valid_o = !empty || in_valid_i;
    assign bypass      = empty && in_valid_i && out_ready_i;
    assign out_data_o  = empty ? in_data_i : mem[rd_q];
    assign do_write    = in_valid_i && in_ready_o && !bypass;
    assign do_read     = out_ready_i && !empty;

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0;
        end else begin
            if (do_write) begin mem[wr_q] <= in_data_i; wr_q <= wr_q + 2'd1; end
            if (do_read) rd_q <= rd_q + 2'd1;
            count_q <= count_q + 3'(do_write) - 3'(do_read);
        end
    end
endmodule
