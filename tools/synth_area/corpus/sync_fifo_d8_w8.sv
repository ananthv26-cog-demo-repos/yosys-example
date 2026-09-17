// sync_fifo instantiated with Depth=8, Width=8 (see ../examples/sync_fifo.sv).
module sync_fifo_d8_w8 (
    input  logic          clk_i,
    input  logic          rst_ni,
    input  logic          push_i,
    input  logic [7:0]  data_i,
    input  logic          pop_i,
    output logic [7:0]  data_o,
    output logic          full_o,
    output logic          empty_o,
    output logic [3:0]    count_o
);
    sync_fifo #(.Width(8), .Depth(8)) u_fifo (.*);
endmodule
