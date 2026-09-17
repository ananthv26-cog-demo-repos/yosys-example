// interface with modports passed to a submodule
interface p07_if; logic v; logic [3:0] d; modport m (input v, d); modport s (output v, d); endinterface
module p07_sink (input logic clk, p07_if.m i, output logic [3:0] y);
    always_ff @(posedge clk) if (i.v) y <= i.d;
endmodule
module p07_interface_modport (input logic clk, input logic v, input logic [3:0] d, output logic [3:0] y);
    p07_if bus();
    assign bus.v = v; assign bus.d = d;
    p07_sink u (.clk(clk), .i(bus), .y(y));
endmodule
