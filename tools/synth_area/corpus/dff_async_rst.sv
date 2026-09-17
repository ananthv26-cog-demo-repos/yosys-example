// Hand count: 1 DFF with async active-low reset ($_DFF_PN0_), 0 gates.
module dff_async_rst (input logic clk, rst_n, d, output logic q);
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= d;
    end
endmodule
