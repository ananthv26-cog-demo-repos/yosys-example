// Circular FIFO whose empty/full status is an explicit enum state machine (EMPTY/MID/FULL)
// instead of being decoded from a counter. Depth 4, width 8.
// Flops: 32 mem + 2 wr_q + 2 rd_q + 3 state = 39; the 2-bit enum is re-encoded as one-hot flags
// (full_o / empty_o flops plus one state bit) by opt/fsm.
module fifo_enum_state_d4_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    typedef enum logic [1:0] {EMPTY = 2'd0, MID = 2'd1, FULL = 2'd2} state_e;

    logic [7:0] mem [4];
    logic [1:0] wr_q, rd_q, wr_n, rd_n;
    state_e     state_q, state_n;
    logic do_push, do_pop;

    assign full_o  = state_q == FULL;
    assign empty_o = state_q == EMPTY;
    assign do_push = push_i && !full_o;
    assign do_pop  = pop_i  && !empty_o;
    assign data_o  = mem[rd_q];
    assign wr_n    = wr_q + 2'(do_push);
    assign rd_n    = rd_q + 2'(do_pop);

    always_comb begin
        state_n = state_q;
        unique case (state_q)
            EMPTY:   if (do_push) state_n = MID;
            MID:     if (do_push && !do_pop && wr_n == rd_q) state_n = FULL;
                     else if (do_pop && !do_push && rd_n == wr_q) state_n = EMPTY;
            FULL:    if (do_pop) state_n = MID;
            default: state_n = EMPTY;
        endcase
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; state_q <= EMPTY;
        end else begin
            if (do_push) mem[wr_q] <= data_i;
            wr_q <= wr_n; rd_q <= rd_n; state_q <= state_n;
        end
    end
endmodule
