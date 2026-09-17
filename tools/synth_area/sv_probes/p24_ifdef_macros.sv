// `define with args, `ifdef/`else, `include-free macro usage
`define REG(q, d) always_ff @(posedge clk) q <= d
module p24_ifdef_macros (input logic clk, input logic [3:0] d, output logic [3:0] q);
`ifdef INVERT
    `REG(q, ~d);
`else
    `REG(q, d);
`endif
endmodule
