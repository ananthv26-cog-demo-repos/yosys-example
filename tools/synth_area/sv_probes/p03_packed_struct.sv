// typedef packed struct, member access on ports and internally
module p03_packed_struct (input logic clk, input logic [23:0] in, output logic [15:0] y, output logic [7:0] tag);
    typedef struct packed { logic [7:0] tag; logic [15:0] data; } pkt_t;
    pkt_t p_q;
    always_ff @(posedge clk) p_q <= pkt_t'(in);
    assign y = p_q.data; assign tag = p_q.tag;
endmodule
