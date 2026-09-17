// multi-dimensional packed arrays and part-selects
module p18_multidim_packed (input logic [3:0][7:0] a, input logic [1:0] i, output logic [7:0] y, output logic [3:0] hi);
    assign y  = a[i];
    assign hi = a[i][7 -: 4];
endmodule
