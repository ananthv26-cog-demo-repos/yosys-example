// packed union
module p19_packed_union (input logic [15:0] a, output logic [7:0] lo);
    typedef union packed { logic [15:0] word; struct packed { logic [7:0] hi; logic [7:0] lo; } b; } u_t;
    u_t u;
    assign u.word = a;
    assign lo = u.b.lo;
endmodule
