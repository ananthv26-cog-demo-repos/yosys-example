// 4-bit free-running counter with sync reset: 4 DFF + incrementer/reset logic.
module counter4 (input logic clk, rst, output logic [3:0] count);
    always_ff @(posedge clk) begin
        if (rst) count <= '0;
        else     count <= count + 4'd1;
    end
endmodule
