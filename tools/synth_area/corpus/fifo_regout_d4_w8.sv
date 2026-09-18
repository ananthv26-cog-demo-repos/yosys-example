// Circular FIFO with a registered output stage: data_o/valid_o come from flops loaded on pop,
// so the memory read is not on the output path. Depth 4, width 8.
module fifo_regout_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       valid_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [7:0] mem [4];
    logic [1:0] wr_q, rd_q;
    logic [2:0] count_q;
    logic [7:0] data_q;
    logic       valid_q;
    logic do_push, do_pop;

    assign full_o  = count_q == 3'd4;
    assign empty_o = count_q == 3'd0;
    assign do_push = push_i && !full_o;
    assign do_pop  = pop_i  && !empty_o;
    assign data_o  = data_q;
    assign valid_o = valid_q;

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0; valid_q <= 1'b0;
        end else begin
            if (do_push) begin mem[wr_q] <= data_i; wr_q <= wr_q + 2'd1; end
            if (do_pop) begin data_q <= mem[rd_q]; rd_q <= rd_q + 2'd1; end
            valid_q <= do_pop;
            count_q <= count_q + 3'(do_push) - 3'(do_pop);
        end
    end
endmodule
