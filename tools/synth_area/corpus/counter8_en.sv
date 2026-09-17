// 8-bit counter with enable and sync reset: 8 DFF.
module counter8_en (input logic clk, rst, en, output logic [7:0] count);
    always_ff @(posedge clk) begin
        if (rst)     count <= '0;
        else if (en) count <= count + 8'd1;
    end
endmodule
