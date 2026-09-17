// SV data types: logic, bit, byte, shortint, int, unsigned, 4-state literals
module p02_sv_types (input logic clk, input byte a, input shortint b, output int y);
    bit [7:0] r;
    always_ff @(posedge clk) r <= a + 8'(b);
    assign y = int'(r) + 32'd1;
endmodule
