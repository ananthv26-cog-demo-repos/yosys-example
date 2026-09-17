// unpacked array port and array-of-array memory
module p04_unpacked_array_port (input logic clk, input logic [7:0] in [4], input logic [1:0] sel, output logic [7:0] y);
    logic [7:0] m [4];
    always_ff @(posedge clk) m <= in;
    assign y = m[sel];
endmodule
