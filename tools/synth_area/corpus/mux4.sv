// Hand count: 3 MUX (two first-level, one second-level), depth 2.
module mux4 (input logic [3:0] d, input logic [1:0] sel, output logic y);
    assign y = d[sel];
endmodule
