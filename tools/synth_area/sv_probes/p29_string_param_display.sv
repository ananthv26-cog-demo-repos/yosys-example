// string parameter + initial $display (should synthesize to nothing extra)
module p29_string_param_display #(parameter string Name = "blk") (input logic clk, input logic a, output logic y);
    initial $display("hello from %s", Name);
    always_ff @(posedge clk) y <= a;
endmodule
