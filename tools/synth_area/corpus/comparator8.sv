// 8-bit unsigned comparator, three outputs.
module comparator8 (input logic [7:0] a, b, output logic lt, eq, gt);
    assign lt = a < b;
    assign eq = a == b;
    assign gt = a > b;
endmodule
