// 8-to-3 priority encoder with valid: unique casez-free loop form.
module priority_enc8 (input logic [7:0] req, output logic [2:0] idx, output logic valid);
    always_comb begin
        idx   = '0;
        valid = 1'b0;
        for (int i = 0; i < 8; i++) begin
            if (req[i]) begin
                idx   = 3'(i);
                valid = 1'b1;
            end
        end
    end
endmodule
