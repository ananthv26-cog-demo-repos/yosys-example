// Hand count: 8-input XOR reduce = 7 XOR/XNOR gates; balanced tree depth 3.
module parity8 (input logic [7:0] d, output logic p);
    assign p = ^d;
endmodule
