// Hand count: 1 DFF + the sync-reset AND (d & ~rst): 2 gates (NOT+AND or NOR+NOT form).
module dff_sync_rst (input logic clk, rst, d, output logic q);
    always_ff @(posedge clk) begin
        if (rst) q <= 1'b0;
        else     q <= d;
    end
endmodule
