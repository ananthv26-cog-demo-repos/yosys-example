// sync_fifo instantiated with Depth=64, Width=32 (see ../sync_fifo.sv).
module sync_fifo_d64_w32 (
    input  logic          clk_i,
    input  logic          rst_ni,
    input  logic          push_i,
    input  logic [31:0]   data_i,
    input  logic          pop_i,
    output logic [31:0]   data_o,
    output logic          full_o,
    output logic          empty_o,
    output logic [6:0]    count_o
);
    sync_fifo #(.Width(32), .Depth(64)) u_fifo (.*);
endmodule
