// Single-clock FIFO whose pointers are kept in Gray code (binary shadow for the increment,
// Gray for the compare, as in a CDC FIFO but without the second clock). Depth 8, width 8.
// Flops: 64 mem + 4x4 pointers = 80, minus 2: the Gray MSB equals the binary MSB, so opt_merge
// folds wr_gray_q[3]/rd_gray_q[3] into wr_bin_q[3]/rd_bin_q[3].
module fifo_gray_ptr_d8_w8 (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       push_i,
    input  logic [7:0] data_i,
    input  logic       pop_i,
    output logic [7:0] data_o,
    output logic       full_o,
    output logic       empty_o
);
    logic [7:0] mem [8];
    logic [3:0] wr_bin_q, rd_bin_q, wr_gray_q, rd_gray_q;
    logic [3:0] wr_bin_n, rd_bin_n;

    function automatic logic [3:0] bin2gray(input logic [3:0] b);
        return b ^ (b >> 1);
    endfunction

    assign empty_o  = wr_gray_q == rd_gray_q;
    assign full_o   = wr_gray_q == {~rd_gray_q[3:2], rd_gray_q[1:0]};
    assign data_o   = mem[rd_bin_q[2:0]];
    assign wr_bin_n = wr_bin_q + 4'(push_i && !full_o);
    assign rd_bin_n = rd_bin_q + 4'(pop_i && !empty_o);

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            wr_bin_q <= '0; rd_bin_q <= '0; wr_gray_q <= '0; rd_gray_q <= '0;
        end else begin
            if (push_i && !full_o) mem[wr_bin_q[2:0]] <= data_i;
            wr_bin_q <= wr_bin_n; wr_gray_q <= bin2gray(wr_bin_n);
            rd_bin_q <= rd_bin_n; rd_gray_q <= bin2gray(rd_bin_n);
        end
    end
endmodule
