// Shift-register FIFO (data moves toward the head): depth 4, width 8.
module fifo_shift_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [7:0] q [4];
    logic [2:0] count_q;
    logic do_push, do_pop;

    assign full_o  = count_q == 3'd4;
    assign empty_o = count_q == 3'd0;
    assign do_push = push_i && !full_o;
    assign do_pop  = pop_i  && !empty_o;
    assign data_o  = q[0];

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            count_q <= '0;
        end else begin
            count_q <= count_q + 3'(do_push) - 3'(do_pop);
            if (do_pop) begin
                for (int i = 0; i < 3; i++) q[i] <= q[i+1];
                if (do_push) q[count_q - 1] <= data_i;
            end else if (do_push) begin
                q[count_q[1:0]] <= data_i;
            end
        end
    end
endmodule
