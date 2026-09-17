`timescale 1ns/1ps
module tb;
  integer seed = 1;
  integer cycle = 0;
  integer mismatches = 0;
  integer gate_x_bits = 0;
  integer compared_bits = 0;
  integer i;
  reg clk;
  reg rst;
  reg en;
  wire [7:0] count_gold;
  wire [7:0] count__gate;
  counter8_en gold(.clk(clk), .rst(rst), .en(en), .count(count_gold));
  counter8_en__gate gate(.clk(clk), .rst(rst), .en(en), .count(count__gate));
  initial begin
    clk = 0;
    rst = 1;
    en = 0;
  end
  always #5 clk = ~clk;
  always @(negedge clk) begin
    if (cycle >= 4) begin
      for (i = 0; i < 8; i = i + 1) begin
        if (count_gold[i] !== 1'bx && count_gold[i] !== 1'bz) begin
          compared_bits = compared_bits + 1;
          if (count__gate[i] === 1'bx || count__gate[i] === 1'bz) gate_x_bits = gate_x_bits + 1;
          else if (count__gate[i] !== count_gold[i]) begin
            mismatches = mismatches + 1;
            if (mismatches <= 10) $display("MISMATCH cycle=%0d count[%0d] rtl=%b gate=%b", cycle, i, count_gold[i], count__gate[i]);
          end
        end
      end
    end
    cycle = cycle + 1;
    en = $random(seed) & {1{1'b1}};
    rst = (cycle < 4 || ($random(seed) & 63) == 0) ? 1'b1 : 1'b0;
    if (cycle == 200) begin
      $display("DIFFSIM cycles=%0d compared_bits=%0d mismatches=%0d gate_x_bits=%0d", cycle, compared_bits, mismatches, gate_x_bits);
      $finish;
    end
  end
endmodule
