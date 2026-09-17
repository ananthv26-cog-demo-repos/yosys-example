// concurrent SVA property/assert with |=> and $past
module p15_concurrent_sva (input logic clk, rst_n, req, output logic ack);
    always_ff @(posedge clk or negedge rst_n) if (!rst_n) ack <= 1'b0; else ack <= req;
    property p_ack; @(posedge clk) disable iff (!rst_n) req |=> ack; endproperty
    a_ack: assert property (p_ack);
endmodule
