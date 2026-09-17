// 8-bit Fibonacci LFSR (taps 8,6,5,4): 8 DFF + 3 XOR, reset loads a non-zero seed.
module lfsr8 (input logic clk, rst, output logic [7:0] q);
    logic fb;
    assign fb = q[7] ^ q[5] ^ q[4] ^ q[3];
    always_ff @(posedge clk) begin
        if (rst) q <= 8'h1;
        else     q <= {q[6:0], fb};
    end
endmodule
