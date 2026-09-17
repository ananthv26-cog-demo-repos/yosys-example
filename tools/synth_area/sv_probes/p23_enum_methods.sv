// enum methods .next()/.first() and enum casts
module p23_enum_methods (input logic clk, rst_n, output logic [1:0] y);
    typedef enum logic [1:0] {A, B, C} e_t;
    e_t s;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) s <= s.first(); else s <= (s == C) ? s.first() : s.next();
    assign y = s;
endmodule
