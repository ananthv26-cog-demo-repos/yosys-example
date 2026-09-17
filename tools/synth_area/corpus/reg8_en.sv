// Hand count: 8 DFF + 8 MUX (enable = hold/load select), depth 1.
module reg8_en (input logic clk, en, input logic [7:0] d, output logic [7:0] q);
    always_ff @(posedge clk) if (en) q <= d;
endmodule
