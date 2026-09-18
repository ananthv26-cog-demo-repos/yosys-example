// Packet FIFO: every entry carries a `last` sideband bit and the FIFO counts complete packets,
// so the consumer can wait for a whole packet before popping. Depth 4, width 8 (+1 last bit).
module fifo_last_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       last_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       last_o,
    output logic       full_o,
    output logic       empty_o,
    output logic [2:0] packets_o
);
    logic [8:0] mem [4];   // {last, data}
    logic [1:0] wr_q, rd_q;
    logic [2:0] count_q, packets_q;
    logic do_push, do_pop;

    assign full_o    = count_q == 3'd4;
    assign empty_o   = count_q == 3'd0;
    assign do_push   = push_i && !full_o;
    assign do_pop    = pop_i  && !empty_o;
    assign {last_o, data_o} = mem[rd_q];
    assign packets_o = packets_q;

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0; packets_q <= '0;
        end else begin
            if (do_push) begin mem[wr_q] <= {last_i, data_i}; wr_q <= wr_q + 2'd1; end
            if (do_pop) rd_q <= rd_q + 2'd1;
            count_q   <= count_q + 3'(do_push) - 3'(do_pop);
            packets_q <= packets_q + 3'(do_push && last_i) - 3'(do_pop && last_o);
        end
    end
endmodule
