// FIFO whose storage is a generate-for of per-slot registers with individual write enables
// (no unpacked memory array), read through a case mux. Depth 4, width 8.
module fifo_generate_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [1:0] wr_q, rd_q;
    logic [2:0] count_q;
    logic do_push, do_pop;
    logic [7:0] slot_q [4];

    assign full_o  = count_q == 3'd4;
    assign empty_o = count_q == 3'd0;
    assign do_push = push_i && !full_o;
    assign do_pop  = pop_i  && !empty_o;

    for (genvar i = 0; i < 4; i++) begin : g_slot
        logic we;
        assign we = do_push && (wr_q == 2'(i));
        always_ff @(posedge clk_i) if (we) slot_q[i] <= data_i;
    end

    always_comb begin
        unique case (rd_q)
            2'd0: data_o = slot_q[0];
            2'd1: data_o = slot_q[1];
            2'd2: data_o = slot_q[2];
            2'd3: data_o = slot_q[3];
        endcase
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; count_q <= '0;
        end else begin
            if (do_push) wr_q <= wr_q + 2'd1;
            if (do_pop)  rd_q <= rd_q + 2'd1;
            count_q <= count_q + 3'(do_push) - 3'(do_pop);
        end
    end
endmodule
