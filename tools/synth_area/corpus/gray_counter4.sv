// 4-bit Gray-code counter: binary counter + XOR gray encoding on the output.
module gray_counter4 (input logic clk, rst, output logic [3:0] gray);
    logic [3:0] bin;
    always_ff @(posedge clk) begin
        if (rst) bin <= '0;
        else     bin <= bin + 4'd1;
    end
    assign gray = bin ^ (bin >> 1);
endmodule
