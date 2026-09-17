// array of packed structs as memory with struct-member write
module p26_struct_array_memory (input logic clk, we, input logic [1:0] wa, ra, input logic [7:0] d, output logic [7:0] q, output logic v);
    typedef struct packed { logic valid; logic [7:0] data; } e_t;
    e_t m [4];
    always_ff @(posedge clk) if (we) begin m[wa].data <= d; m[wa].valid <= 1'b1; end
    assign q = m[ra].data; assign v = m[ra].valid;
endmodule
