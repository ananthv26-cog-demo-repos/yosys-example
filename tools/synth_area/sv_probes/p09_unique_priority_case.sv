// unique case / priority if / case inside
module p09_unique_priority_case (input logic [3:0] a, output logic [1:0] y, z);
    always_comb begin
        unique case (a)
            4'd0, 4'd1: y = 2'd0;
            4'd2:       y = 2'd1;
            default:    y = 2'd2;
        endcase
        priority if (a[3]) z = 2'd3; else if (a[2]) z = 2'd2; else z = 2'd0;
    end
endmodule
