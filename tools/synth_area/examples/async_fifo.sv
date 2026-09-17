// Dual-clock FIFO with gray-code pointers and 2-flop synchronizers.
module async_fifo #(
    parameter int unsigned Width = 8,
    parameter int unsigned AddrW = 4
) (
    input  logic             wclk_i,
    input  logic             wrst_ni,
    input  logic             wpush_i,
    input  logic [Width-1:0] wdata_i,
    output logic             wfull_o,
    input  logic             rclk_i,
    input  logic             rrst_ni,
    input  logic             rpop_i,
    output logic [Width-1:0] rdata_o,
    output logic             rempty_o
);
    function automatic logic [AddrW:0] bin2gray(input logic [AddrW:0] b);
        return b ^ (b >> 1);
    endfunction

    logic [Width-1:0] mem [2**AddrW];

    logic [AddrW:0] wbin_q, wgray_q, wq2_rgray_q, wq1_rgray_q;
    logic [AddrW:0] rbin_q, rgray_q, rq2_wgray_q, rq1_wgray_q;

    // write side
    logic [AddrW:0] wbin_n, wgray_n;
    always_comb begin
        wbin_n  = wbin_q + (AddrW + 1)'(wpush_i && !wfull_o);
        wgray_n = bin2gray(wbin_n);
    end
    always_ff @(posedge wclk_i or negedge wrst_ni) begin
        if (!wrst_ni) begin
            wbin_q <= '0; wgray_q <= '0; wfull_o <= 1'b0;
            {wq2_rgray_q, wq1_rgray_q} <= '0;
        end else begin
            wbin_q  <= wbin_n;
            wgray_q <= wgray_n;
            {wq2_rgray_q, wq1_rgray_q} <= {wq1_rgray_q, rgray_q};
            wfull_o <= (wgray_n == {~wq2_rgray_q[AddrW:AddrW-1], wq2_rgray_q[AddrW-2:0]});
        end
    end
    always_ff @(posedge wclk_i) begin
        if (wpush_i && !wfull_o) mem[wbin_q[AddrW-1:0]] <= wdata_i;
    end

    // read side
    logic [AddrW:0] rbin_n, rgray_n;
    always_comb begin
        rbin_n  = rbin_q + (AddrW + 1)'(rpop_i && !rempty_o);
        rgray_n = bin2gray(rbin_n);
        rdata_o = mem[rbin_q[AddrW-1:0]];
    end
    always_ff @(posedge rclk_i or negedge rrst_ni) begin
        if (!rrst_ni) begin
            rbin_q <= '0; rgray_q <= '0; rempty_o <= 1'b1;
            {rq2_wgray_q, rq1_wgray_q} <= '0;
        end else begin
            rbin_q  <= rbin_n;
            rgray_q <= rgray_n;
            {rq2_wgray_q, rq1_wgray_q} <= {rq1_wgray_q, wgray_q};
            rempty_o <= (rgray_n == rq2_wgray_q);
        end
    end
endmodule
