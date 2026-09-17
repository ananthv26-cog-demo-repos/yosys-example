// Hand count: 8 DFF, 0 gates, every data fanout 1.
module shift_reg8 (input logic clk, d, output logic q);
    logic [7:0] sr;
    always_ff @(posedge clk) sr <= {sr[6:0], d};
    assign q = sr[7];
endmodule
