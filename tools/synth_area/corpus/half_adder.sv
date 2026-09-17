// Hand count: 1 XOR + 1 AND = 2 gates, depth 1.
module half_adder (input logic a, b, output logic sum, carry);
    assign sum   = a ^ b;
    assign carry = a & b;
endmodule
