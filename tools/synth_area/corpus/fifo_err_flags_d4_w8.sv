// Circular FIFO with sticky overflow/underflow error flags (push when full / pop when empty),
// cleared by err_clr_i. Depth 4, width 8.
module fifo_err_flags_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    input  logic       err_clr_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o,
    output logic       overflow_o,
    output logic       underflow_o
);
    logic [7:0] mem [4];
    logic [1:0] wr_q, rd_q;
    logic [2:0] count_q;
    logic       overflow_q, underflow_q;
    logic do_push, do_pop;

    assign full_o      = count_q == 3'd4;
    assign empty_o     = count_q == 3'd0;
    assign do_push     = push_i && !full_o;
    assign do_pop      = pop_i  && !empty_o;
    assign data_o      = mem[rd_q];
    assign overflow_o  = overflow_q;
    assign underflow_o = underflow_q;

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0; overflow_q <= 1'b0; underflow_q <= 1'b0;
        end else begin
            if (do_push) begin mem[wr_q] <= data_i; wr_q <= wr_q + 2'd1; end
            if (do_pop) rd_q <= rd_q + 2'd1;
            count_q <= count_q + 3'(do_push) - 3'(do_pop);
            overflow_q  <= (overflow_q  && !err_clr_i) || (push_i && full_o);
            underflow_q <= (underflow_q && !err_clr_i) || (pop_i && empty_o);
        end
    end
endmodule
