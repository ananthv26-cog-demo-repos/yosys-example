// class declaration (testbench-only construct) next to synthesizable logic
module p30_class_decl (input logic clk, input logic a, output logic y);
    class C; int x; function int get(); return x; endfunction endclass
    always_ff @(posedge clk) y <= a;
endmodule
