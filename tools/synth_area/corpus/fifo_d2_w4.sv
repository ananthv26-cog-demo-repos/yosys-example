// Smallest register-based FIFO: depth 2, width 4 (ping-pong).
module fifo_d2_w4 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [3:0] data_i,
    input  logic       pop_i,
    output logic [3:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [3:0] mem [2];
    logic wr_q, rd_q;
    logic [1:0] count_q;

    assign full_o  = count_q == 2'd2;
    assign empty_o = count_q == 2'd0;
    assign data_o  = mem[rd_q];

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= 1'b0; rd_q <= 1'b0; count_q <= '0;
        end else begin
            if (push_i && !full_o) begin mem[wr_q] <= data_i; wr_q <= ~wr_q; end
            if (pop_i && !empty_o) rd_q <= ~rd_q;
            case ({push_i && !full_o, pop_i && !empty_o})
                2'b10:   count_q <= count_q + 2'd1;
                2'b01:   count_q <= count_q - 2'd1;
                default: ;
            endcase
        end
    end
endmodule
