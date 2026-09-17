// DPI-C import (not synthesizable; expect frontend to reject or ignore)
module p16_dpi_import (input logic clk, input logic [7:0] a, output logic [7:0] y);
    import "DPI-C" function int c_hash(input int x);
    always_ff @(posedge clk) y <= a;
    initial $display("%0d", c_hash(3));
endmodule
