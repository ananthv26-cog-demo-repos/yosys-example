// FIFO whose entry type is a packed struct from a package, with a type
// parameter and a package function used in a localparam.
// Compile with fifo_pkg.sv first.
module struct_fifo
    import fifo_pkg::*;
#(
    parameter int unsigned Depth = 8,
    parameter type data_t = entry_t,
    localparam int unsigned PtrW = ptr_width(Depth)
) (
    input  logic  clk_i,
    input  logic  rst_ni,
    input  logic  push_i,
    input  data_t data_i,
    input  logic  pop_i,
    output data_t data_o,
    output logic  full_o,
    output logic  empty_o,
    output logic  err_at_head_o
);
    data_t            mem_q [Depth];
    logic [PtrW-1:0]  wr_q, rd_q;
    logic [PtrW:0]    cnt_q;

    assign full_o        = (cnt_q == (PtrW + 1)'(Depth));
    assign empty_o       = (cnt_q == '0);
    assign data_o        = mem_q[rd_q];
    assign err_at_head_o = !empty_o && (data_o.kind == KIND_ERR);

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_q  <= '0;
            rd_q  <= '0;
            cnt_q <= '0;
        end else begin
            unique case ({push_i && !full_o, pop_i && !empty_o})
                2'b10: begin
                    mem_q[wr_q] <= data_i;
                    wr_q  <= (wr_q == PtrW'(Depth - 1)) ? '0 : wr_q + 1'b1;
                    cnt_q <= cnt_q + 1'b1;
                end
                2'b01: begin
                    rd_q  <= (rd_q == PtrW'(Depth - 1)) ? '0 : rd_q + 1'b1;
                    cnt_q <= cnt_q - 1'b1;
                end
                2'b11: begin
                    mem_q[wr_q] <= data_i;
                    wr_q  <= (wr_q == PtrW'(Depth - 1)) ? '0 : wr_q + 1'b1;
                    rd_q  <= (rd_q == PtrW'(Depth - 1)) ? '0 : rd_q + 1'b1;
                end
                default: ;
            endcase
        end
    end
endmodule
