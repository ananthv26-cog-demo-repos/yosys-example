// loop variable declared in for header, compound assignment, ++
module p20_int_loop_var (input logic [7:0] a, output logic [3:0] cnt);
    always_comb begin
        cnt = '0;
        for (int i = 0; i < 8; i++) cnt += 4'(a[i]);
    end
endmodule
