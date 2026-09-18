// Elastic pipeline FIFO: three chained valid/ready stages, each a 1-entry register with its
// own full flag (data ripples toward the output whenever the next stage is free). Depth 3, width 8.
// Flops: 3x8 data + 3 full = 27. The unreset data registers are only meaningful while their stage is
// full, so k-induction cannot pair them and the flow falls back to the bounded proof from reset.
module fifo_pipe_d3_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       valid_i,
    output logic       ready_o,
    input  logic [7:0] data_i,
    output logic       valid_o,
    input  logic       ready_i,
    output logic [7:0] data_o
);
    logic [7:0] data_q [3];
    logic [2:0] full_q;
    logic [3:0] ready;   // ready[i] = stage i can accept; ready[3] is the downstream consumer

    always_comb begin
        ready[3] = ready_i;
        for (int i = 2; i >= 0; i--) ready[i] = !full_q[i] || ready[i+1];
    end
    assign ready_o = ready[0];
    assign valid_o = full_q[2];
    assign data_o  = data_q[2];

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            full_q <= '0;
        end else begin
            for (int i = 0; i < 3; i++) begin
                if (ready[i]) begin
                    full_q[i] <= (i == 0) ? valid_i : full_q[i-1];
                    if ((i == 0) ? valid_i : full_q[i-1]) data_q[i] <= (i == 0) ? data_i : data_q[i-1];
                end
            end
        end
    end
endmodule
