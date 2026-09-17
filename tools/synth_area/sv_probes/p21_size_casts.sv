// size/sign casts and system functions $bits/$size/$clog2
module p21_size_casts (input logic [11:0] a, input logic signed [7:0] s, output logic [15:0] y, output logic [3:0] n);
    localparam int L = $clog2(12);
    assign y = 16'(a) + 16'(signed'(s));
    assign n = 4'($bits(a) + L);
endmodule
