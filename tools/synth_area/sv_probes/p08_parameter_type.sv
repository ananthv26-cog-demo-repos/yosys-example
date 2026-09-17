// type parameters and parameterized type widths
module p08_reg #(parameter type T = logic [7:0]) (input logic clk, input T d, output T q);
    always_ff @(posedge clk) q <= d;
endmodule
module p08_parameter_type (input logic clk, input logic [15:0] d, output logic [15:0] q);
    p08_reg #(.T(logic [15:0])) u (.clk, .d, .q);
endmodule
