// Circular FIFO with a synchronous active-high reset (no async reset anywhere): depth 4, width 8.
// Flops: 32 mem + 2 wr_q + 2 rd_q + 3 count_q = 39, plus 2 for a registered copy of rd_q that
// memory_dff folds into the read port (sync-reset pointers can be absorbed, async-reset ones cannot).
module fifo_sync_rst_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_i,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [7:0] mem [4];
    logic [1:0] wr_q, rd_q;
    logic [2:0] count_q;
    logic do_push, do_pop;

    assign full_o  = count_q == 3'd4;
    assign empty_o = count_q == 3'd0;
    assign do_push = push_i && !full_o;
    assign do_pop  = pop_i  && !empty_o;
    assign data_o  = mem[rd_q];

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0;
        end else begin
            if (do_push) begin mem[wr_q] <= data_i; wr_q <= wr_q + 2'd1; end
            if (do_pop) rd_q <= rd_q + 2'd1;
            count_q <= count_q + 3'(do_push) - 3'(do_pop);
        end
    end
endmodule
