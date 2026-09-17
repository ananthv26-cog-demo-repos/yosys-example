// 8-bit ripple/CLA adder with carry out: purely combinational, 9 output bits.
module adder8 (input logic [7:0] a, b, output logic [7:0] sum, output logic cout);
    assign {cout, sum} = a + b;
endmodule
