// 1-entry valid/ready skid buffer (the smallest FIFO).
module skid_buffer #(parameter int unsigned Width = 8) (
    input  logic             clk_i,
    input  logic             rst_ni,
    input  logic             valid_i,
    output logic             ready_o,
    input  logic [Width-1:0] data_i,
    output logic             valid_o,
    input  logic             ready_i,
    output logic [Width-1:0] data_o
);
    logic             full_q;
    logic [Width-1:0] data_q;

    assign ready_o = !full_q || ready_i;
    assign valid_o = full_q;
    assign data_o  = data_q;

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            full_q <= 1'b0;
        end else if (ready_o) begin
            full_q <= valid_i;
            if (valid_i) data_q <= data_i;
        end
    end
endmodule
