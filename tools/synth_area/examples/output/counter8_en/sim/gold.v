module counter8_en (
	clk,
	rst,
	en,
	count
);
	input wire clk;
	input wire rst;
	input wire en;
	output reg [7:0] count;
	always @(posedge clk)
		if (rst)
			count <= 1'sb0;
		else if (en)
			count <= count + 8'd1;
endmodule
