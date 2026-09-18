// Circular FIFO with programmable almost-full / almost-empty thresholds. Depth 8, width 8.
module fifo_almost_flags_d8_w8 #(
    parameter int unsigned AlmostFull  = 6,
    parameter int unsigned AlmostEmpty = 2
) (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o,
    output logic       almost_full_o,
    output logic       almost_empty_o,
    output logic [3:0] count_o
);
    logic [7:0] mem [8];
    logic [2:0] wr_q, rd_q;
    logic [3:0] count_q;
    logic do_push, do_pop;

    assign full_o         = count_q == 4'd8;
    assign empty_o        = count_q == 4'd0;
    assign almost_full_o  = count_q >= 4'(AlmostFull);
    assign almost_empty_o = count_q <= 4'(AlmostEmpty);
    assign count_o        = count_q;
    assign do_push        = push_i && !full_o;
    assign do_pop         = pop_i  && !empty_o;
    assign data_o         = mem[rd_q];

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0;
        end else begin
            if (do_push) begin mem[wr_q] <= data_i; wr_q <= wr_q + 3'd1; end
            if (do_pop) rd_q <= rd_q + 3'd1;
            count_q <= count_q + 4'(do_push) - 4'(do_pop);
        end
    end
endmodule
