// automatic function with loop, break, local variables, return
module p11_function_automatic (input logic [7:0] a, output logic [3:0] y);
    function automatic logic [3:0] first_one(input logic [7:0] v);
        first_one = 4'd8;
        for (int i = 0; i < 8; i++) if (v[i]) begin first_one = 4'(i); break; end
    endfunction
    assign y = first_one(a);
endmodule
