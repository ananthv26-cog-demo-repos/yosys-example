// generate for/if with named blocks and genvar in localparam
module p12_generate #(parameter int N = 4) (input logic clk, input logic [N-1:0] d, output logic [N-1:0] q);
    for (genvar i = 0; i < N; i++) begin : g
        if (i % 2 == 0) begin : even
            always_ff @(posedge clk) q[i] <= d[i];
        end else begin : odd
            always_ff @(posedge clk) q[i] <= ~d[i];
        end
    end
endmodule
