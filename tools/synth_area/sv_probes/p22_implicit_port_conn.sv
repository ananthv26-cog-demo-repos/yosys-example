// .name and .* implicit port connections
module p22_leaf (input logic clk, input logic [3:0] d, output logic [3:0] q);
    always_ff @(posedge clk) q <= d;
endmodule
module p22_implicit_port_conn (input logic clk, input logic [3:0] d, output logic [3:0] q);
    p22_leaf u (.*);
endmodule
