`timescale 1ns/1ps
module tb;
  integer seed = 1;
  integer cycle = 0;
  integer mismatches = 0;
  integer gate_x_bits = 0;
  integer compared_bits = 0;
  integer i;
  reg clk_i;
  reg rst_ni;
  reg push_i;
  reg [31:0] data_i;
  reg pop_i;
  wire [31:0] data_o_gold;
  wire [31:0] data_o__gate;
  wire full_o_gold;
  wire full_o__gate;
  wire empty_o_gold;
  wire empty_o__gate;
  wire [4:0] count_o_gold;
  wire [4:0] count_o__gate;
  sync_fifo gold(.clk_i(clk_i), .rst_ni(rst_ni), .push_i(push_i), .data_i(data_i), .pop_i(pop_i), .data_o(data_o_gold), .full_o(full_o_gold), .empty_o(empty_o_gold), .count_o(count_o_gold));
  sync_fifo__gate gate(.clk_i(clk_i), .rst_ni(rst_ni), .push_i(push_i), .data_i(data_i), .pop_i(pop_i), .data_o(data_o__gate), .full_o(full_o__gate), .empty_o(empty_o__gate), .count_o(count_o__gate));
  initial begin
    clk_i = 0;
    rst_ni = 0;
    push_i = 0;
    data_i = 0;
    pop_i = 0;
  end
  always #5 clk_i = ~clk_i;
  always @(negedge clk_i) begin
    if (cycle >= 4) begin
      for (i = 0; i < 32; i = i + 1) begin
        if (data_o_gold[i] !== 1'bx && data_o_gold[i] !== 1'bz) begin
          compared_bits = compared_bits + 1;
          if (data_o__gate[i] === 1'bx || data_o__gate[i] === 1'bz) gate_x_bits = gate_x_bits + 1;
          else if (data_o__gate[i] !== data_o_gold[i]) begin
            mismatches = mismatches + 1;
            if (mismatches <= 10) $display("MISMATCH cycle=%0d data_o[%0d] rtl=%b gate=%b", cycle, i, data_o_gold[i], data_o__gate[i]);
          end
        end
      end
      for (i = 0; i < 1; i = i + 1) begin
        if (full_o_gold !== 1'bx && full_o_gold !== 1'bz) begin
          compared_bits = compared_bits + 1;
          if (full_o__gate === 1'bx || full_o__gate === 1'bz) gate_x_bits = gate_x_bits + 1;
          else if (full_o__gate !== full_o_gold) begin
            mismatches = mismatches + 1;
            if (mismatches <= 10) $display("MISMATCH cycle=%0d full_o[%0d] rtl=%b gate=%b", cycle, i, full_o_gold, full_o__gate);
          end
        end
      end
      for (i = 0; i < 1; i = i + 1) begin
        if (empty_o_gold !== 1'bx && empty_o_gold !== 1'bz) begin
          compared_bits = compared_bits + 1;
          if (empty_o__gate === 1'bx || empty_o__gate === 1'bz) gate_x_bits = gate_x_bits + 1;
          else if (empty_o__gate !== empty_o_gold) begin
            mismatches = mismatches + 1;
            if (mismatches <= 10) $display("MISMATCH cycle=%0d empty_o[%0d] rtl=%b gate=%b", cycle, i, empty_o_gold, empty_o__gate);
          end
        end
      end
      for (i = 0; i < 5; i = i + 1) begin
        if (count_o_gold[i] !== 1'bx && count_o_gold[i] !== 1'bz) begin
          compared_bits = compared_bits + 1;
          if (count_o__gate[i] === 1'bx || count_o__gate[i] === 1'bz) gate_x_bits = gate_x_bits + 1;
          else if (count_o__gate[i] !== count_o_gold[i]) begin
            mismatches = mismatches + 1;
            if (mismatches <= 10) $display("MISMATCH cycle=%0d count_o[%0d] rtl=%b gate=%b", cycle, i, count_o_gold[i], count_o__gate[i]);
          end
        end
      end
    end
    cycle = cycle + 1;
    push_i <= $random(seed) & {1{1'b1}};
    data_i <= $random(seed);
    pop_i <= $random(seed) & {1{1'b1}};
    rst_ni <= (cycle < 4 || ($random(seed) & 63) == 0) ? 1'b0 : 1'b1;
    if (cycle == 200) begin
      $display("DIFFSIM cycles=%0d compared_bits=%0d mismatches=%0d gate_x_bits=%0d", cycle, compared_bits, mismatches, gate_x_bits);
      $finish;
    end
  end
endmodule
