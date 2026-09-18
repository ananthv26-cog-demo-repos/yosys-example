// FIFO with one-hot read/write pointers (a rotating enable per slot instead of a binary index):
// no address decode on the write side, an AND-OR mux on the read side. Depth 4, width 8.
module fifo_onehot_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [7:0] slot_q [4];
    logic [3:0] wr_sel_q, rd_sel_q;
    logic [2:0] count_q;
    logic do_push, do_pop;

    assign full_o  = count_q == 3'd4;
    assign empty_o = count_q == 3'd0;
    assign do_push = push_i && !full_o;
    assign do_pop  = pop_i  && !empty_o;

    always_comb begin
        data_o = '0;
        for (int i = 0; i < 4; i++) data_o |= slot_q[i] & {8{rd_sel_q[i]}};
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_sel_q <= 4'b0001; rd_sel_q <= 4'b0001; count_q <= '0;
        end else begin
            if (do_push) wr_sel_q <= {wr_sel_q[2:0], wr_sel_q[3]};
            if (do_pop)  rd_sel_q <= {rd_sel_q[2:0], rd_sel_q[3]};
            count_q <= count_q + 3'(do_push) - 3'(do_pop);
        end
    end

    always_ff @(posedge clk_i) begin
        for (int i = 0; i < 4; i++) if (do_push && wr_sel_q[i]) slot_q[i] <= data_i;
    end
endmodule
