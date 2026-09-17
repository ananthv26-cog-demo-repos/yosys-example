// package with parameters, typedef and function; import into module header
package p06_pkg;
    parameter int W = 8;
    typedef logic [W-1:0] word_t;
    function automatic word_t inc(input word_t x); return x + 1'b1; endfunction
endpackage
module p06_package_import import p06_pkg::*; (input logic clk, input word_t a, output word_t y);
    always_ff @(posedge clk) y <= inc(a);
endmodule
