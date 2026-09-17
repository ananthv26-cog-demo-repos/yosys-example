// 2-to-4 one-hot decoder. Hand count: 4 two-input gates + inverters (NOR/AND forms), depth <= 2.
module dec2to4 (input logic [1:0] a, output logic [3:0] y);
    always_comb begin
        y = '0;
        y[a] = 1'b1;
    end
endmodule
