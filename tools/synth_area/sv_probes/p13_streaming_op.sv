// streaming operator (bit reverse) and replication
module p13_streaming_op (input logic [7:0] a, output logic [7:0] y, output logic [15:0] z);
    assign y = {<<{a}};
    assign z = {2{a}};
endmodule
