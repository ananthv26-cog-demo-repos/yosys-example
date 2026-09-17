// 4-bit ALU: add, sub, and, or, xor, shift; op is 3 bits.
module alu4 (input logic [3:0] a, b, input logic [2:0] op, output logic [3:0] y, output logic zero);
    always_comb begin
        unique case (op)
            3'd0:    y = a + b;
            3'd1:    y = a - b;
            3'd2:    y = a & b;
            3'd3:    y = a | b;
            3'd4:    y = a ^ b;
            3'd5:    y = a << 1;
            3'd6:    y = a >> 1;
            default: y = ~a;
        endcase
        zero = (y == '0);
    end
endmodule
