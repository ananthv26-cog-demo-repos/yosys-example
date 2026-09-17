// always_latch
module p10_always_latch (input logic en, input logic [3:0] d, output logic [3:0] q);
    always_latch if (en) q = d;
endmodule
