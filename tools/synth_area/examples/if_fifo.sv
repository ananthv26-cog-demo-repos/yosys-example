// FIFO whose producer/consumer sides are SystemVerilog interfaces with
// modports. if_fifo_top exposes plain ports so it can be synthesized as a top.
interface fifo_port_if #(parameter int unsigned Width = 16) (input logic clk);
    logic             valid;
    logic             ready;
    logic [Width-1:0] data;
    modport src (output valid, input  ready, output data);
    modport dst (input  valid, output ready, input  data);
endinterface

module if_fifo_core #(
    parameter int unsigned Width = 16,
    parameter int unsigned Depth = 4
) (
    input logic       clk_i,
    input logic       rst_ni,
    fifo_port_if.dst  in,
    fifo_port_if.src  out
);
    localparam int unsigned PtrW = (Depth > 1) ? $clog2(Depth) : 1;
    logic [Width-1:0] mem_q [Depth];
    logic [PtrW-1:0]  wr_q, rd_q;
    logic [PtrW:0]    cnt_q;
    logic full, empty;

    assign full      = (cnt_q == (PtrW + 1)'(Depth));
    assign empty     = (cnt_q == '0);
    assign in.ready  = !full;
    assign out.valid = !empty;
    assign out.data  = mem_q[rd_q];

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q <= '0; rd_q <= '0; cnt_q <= '0;
        end else begin
            if (in.valid && !full) begin
                mem_q[wr_q] <= in.data;
                wr_q <= (wr_q == PtrW'(Depth - 1)) ? '0 : wr_q + 1'b1;
            end
            if (out.valid && out.ready) rd_q <= (rd_q == PtrW'(Depth - 1)) ? '0 : rd_q + 1'b1;
            cnt_q <= cnt_q + (PtrW + 1)'(in.valid && !full) - (PtrW + 1)'(out.valid && out.ready);
        end
    end
endmodule

module if_fifo_top #(
    parameter int unsigned Width = 16,
    parameter int unsigned Depth = 4
) (
    input  logic             clk_i,
    input  logic             rst_ni,
    input  logic             in_valid_i,
    output logic             in_ready_o,
    input  logic [Width-1:0] in_data_i,
    output logic             out_valid_o,
    input  logic             out_ready_i,
    output logic [Width-1:0] out_data_o
);
    fifo_port_if #(.Width(Width)) in_if  (.clk(clk_i));
    fifo_port_if #(.Width(Width)) out_if (.clk(clk_i));

    assign in_if.valid  = in_valid_i;
    assign in_if.data   = in_data_i;
    assign in_ready_o   = in_if.ready;
    assign out_valid_o  = out_if.valid;
    assign out_data_o   = out_if.data;
    assign out_if.ready = out_ready_i;

    if_fifo_core #(.Width(Width), .Depth(Depth)) u_core (
        .clk_i, .rst_ni, .in(in_if), .out(out_if)
    );
endmodule
