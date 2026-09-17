// Hand count: sum = a^b^cin (2 XOR); cout = majority (3 gates, or 1 MUX + 1 gate).
module full_adder (input logic a, b, cin, output logic sum, cout);
    assign sum  = a ^ b ^ cin;
    assign cout = (a & b) | (cin & (a ^ b));
endmodule
