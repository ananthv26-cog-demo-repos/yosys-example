// always_ff / always_comb, logic type, non-blocking reset
module p01_always_ff_comb (input logic clk, rst_n, a, b, output logic y);
    logic t;
    always_comb t = a ^ b;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) y <= 1'b0; else y <= t;
endmodule
