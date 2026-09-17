// enum typedef, enum-typed state register, enum literal comparison
module p05_enum (input logic clk, rst_n, go, done, output logic busy);
    typedef enum logic [1:0] {IDLE, RUN, WAIT} st_e;
    st_e st_q, st_d;
    always_comb begin
        st_d = st_q;
        case (st_q)
            IDLE: if (go) st_d = RUN;
            RUN:  if (done) st_d = WAIT;
            WAIT: st_d = IDLE;
            default: st_d = IDLE;
        endcase
    end
    always_ff @(posedge clk or negedge rst_n) if (!rst_n) st_q <= IDLE; else st_q <= st_d;
    assign busy = (st_q != IDLE);
endmodule
