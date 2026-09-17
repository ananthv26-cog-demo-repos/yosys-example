// parameter of struct type with struct literal default
module p27_parameter_struct (input logic [7:0] a, output logic [7:0] y);
    typedef struct packed { logic [7:0] mask; logic [7:0] add; } cfg_t;
    parameter cfg_t Cfg = '{mask: 8'hF0, add: 8'h01};
    assign y = (a & Cfg.mask) + Cfg.add;
endmodule
