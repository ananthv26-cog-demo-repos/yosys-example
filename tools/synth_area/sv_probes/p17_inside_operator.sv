// inside operator with ranges and wildcard equality
module p17_inside_operator (input logic [7:0] a, output logic y, w);
    assign y = a inside {[8'd10:8'd20], 8'd42};
    assign w = (a ==? 8'b1010_zzzz);
endmodule
