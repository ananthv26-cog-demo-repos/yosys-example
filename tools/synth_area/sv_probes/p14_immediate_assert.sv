// immediate assertion + $error in always_ff (should be ignored for synthesis)
module p14_immediate_assert (input logic clk, input logic [3:0] a, output logic [3:0] y);
    always_ff @(posedge clk) begin
        y <= a;
        assert (a != 4'hF) else $error("saturated");
    end
endmodule
