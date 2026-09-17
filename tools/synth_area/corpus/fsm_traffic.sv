// 3-state Moore FSM with an enumerated state register and a 2-bit timer.
module fsm_traffic (input logic clk, rst, input logic car, output logic red, yellow, green);
    typedef enum logic [1:0] {RED, GREEN, YELLOW} state_e;
    state_e state_q, state_d;
    logic [1:0] timer_q, timer_d;

    always_comb begin
        state_d = state_q;
        timer_d = timer_q + 2'd1;
        unique case (state_q)
            RED:    if (car && timer_q == 2'd3) begin state_d = GREEN;  timer_d = '0; end
            GREEN:  if (timer_q == 2'd3)        begin state_d = YELLOW; timer_d = '0; end
            YELLOW: if (timer_q == 2'd1)        begin state_d = RED;    timer_d = '0; end
            default: state_d = RED;
        endcase
        red    = state_q == RED;
        yellow = state_q == YELLOW;
        green  = state_q == GREEN;
    end

    always_ff @(posedge clk) begin
        if (rst) begin state_q <= RED; timer_q <= '0; end
        else     begin state_q <= state_d; timer_q <= timer_d; end
    end
endmodule
