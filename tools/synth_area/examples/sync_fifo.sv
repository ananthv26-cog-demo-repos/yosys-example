// Plain synchronous FIFO written in everyday SystemVerilog:
// logic types, always_ff/always_comb, $clog2, typed parameters.
module sync_fifo #(
    parameter int unsigned Width = 32,
    parameter int unsigned Depth = 16,
    localparam int unsigned AddrW = (Depth > 1) ? $clog2(Depth) : 1
) (
    input  logic             clk_i,
    input  logic             rst_ni,
    input  logic             push_i,
    input  logic [Width-1:0] data_i,
    input  logic             pop_i,
    output logic [Width-1:0] data_o,
    output logic             full_o,
    output logic             empty_o,
    output logic [AddrW:0]   count_o
);
    logic [Width-1:0] mem [Depth];
    logic [AddrW-1:0] wr_ptr_q, rd_ptr_q;
    logic [AddrW:0]   count_q;

    logic do_push, do_pop;
    always_comb begin
        do_push = push_i && !full_o;
        do_pop  = pop_i  && !empty_o;
        full_o  = (count_q == AddrW'(Depth)) || (count_q[AddrW] && Depth == (1 << AddrW));
        empty_o = (count_q == '0);
        count_o = count_q;
        data_o  = mem[rd_ptr_q];
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_ptr_q <= '0;
            rd_ptr_q <= '0;
            count_q  <= '0;
        end else begin
            if (do_push) begin
                wr_ptr_q <= (wr_ptr_q == AddrW'(Depth - 1)) ? '0 : wr_ptr_q + 1'b1;
            end
            if (do_pop) begin
                rd_ptr_q <= (rd_ptr_q == AddrW'(Depth - 1)) ? '0 : rd_ptr_q + 1'b1;
            end
            case ({do_push, do_pop})
                2'b10:   count_q <= count_q + 1'b1;
                2'b01:   count_q <= count_q - 1'b1;
                default: count_q <= count_q;
            endcase
        end
    end

    always_ff @(posedge clk_i) begin
        if (do_push) mem[wr_ptr_q] <= data_i;
    end
endmodule
