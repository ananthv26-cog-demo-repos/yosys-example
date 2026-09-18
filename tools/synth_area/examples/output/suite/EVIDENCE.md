# Suite evidence

9/9 criteria pass

| | criterion | measured |
|---|---|---|
| PASS | every corpus block runs end to end and meets its hand counts | 49/49 blocks |
| PASS | at least 20 small FIFO variants | 24 FIFO blocks |
| PASS | word-level, sequential, Boolean and mapped report for every design | 49/49 blocks with all four layer files |
| PASS | ASAP7 mapping reports cell counts and summed area | 49/49 blocks, 11658 cells, 1558.19376 um2 total |
| PASS | no unsupported cell silently dropped (an unmodelled cell aborts the run; each run reconciles its layer counts against yosys `stat`) | 0 aborted runs, 0 errors |
| PASS | the Boolean graph and the ASAP7 netlist are proven to match the RTL | 46 proven, 3 bounded, 0 failed, 0 unproven pairs |
| PASS | random RTL vs netlist simulation finds no mismatch | 49/49 blocks simulated, 66012 bits compared, 0 mismatches |
| PASS | report nodes link back to RTL source locations where Yosys has them | `src` on 730/792 (92%) word_level, 95/95 (100%) sequential_overlay, 286/11658 (2%) mapped_cells |
| PASS | the same sources and profile reproduce byte-identical layer files | 49 blocks x 4 layer files byte-identical to the baseline |

## Blocks

| block | layers | cells | area (um2) | equivalence | simulation |
|---|---|---|---|---|---|
| adder8 | 4/4 | 32 | 3.22218 | proven (9/9, 9/9) | 0 mismatches / 1764 bits |
| alu4 | 4/4 | 60 | 5.0301 | proven (5/5, 5/5) | 0 mismatches / 980 bits |
| and2 | 4/4 | 1 | 0.08748 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| comparator8 | 4/4 | 26 | 1.86624 | proven (3/3, 3/3) | 0 mismatches / 588 bits |
| counter4 | 4/4 | 13 | 2.187 | proven (4/4, 4/4) | 0 mismatches / 784 bits |
| counter8_en | 4/4 | 29 | 4.53438 | proven (8/8, 8/8) | 0 mismatches / 1568 bits |
| dec2to4 | 4/4 | 5 | 0.33534 | proven (4/4, 4/4) | 0 mismatches / 784 bits |
| dff | 4/4 | 1 | 0.3645 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| dff_async_rst | 4/4 | 2 | 0.42282 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| dff_sync_rst | 4/4 | 3 | 0.46656 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| fifo_d2_w4 | 4/4 | 64 | 8.019 | proven (18/18, 18/18) | 0 mismatches / 1168 bits |
| fifo_fwft_d4_w8 | 4/4 | 184 | 24.5673 | proven (49/49, 49/49) | 0 mismatches / 1944 bits |
| fifo_shift_d4_w8 | 4/4 | 185 | 23.02182 | bounded (14/38, 37/37) | 0 mismatches / 1944 bits |
| fifo_almost_flags_d8_w8 | 4/4 | 357 | 47.2392 | proven (87/87, 86/86) | 0 mismatches / 3120 bits |
| fifo_bypass_d4_w8 | 4/4 | 223 | 27.702 | proven (50/50, 49/49) | 0 mismatches / 1960 bits |
| fifo_clr_d4_w8 | 4/4 | 210 | 25.52958 | proven (50/50, 49/49) | 0 mismatches / 1832 bits |
| fifo_enum_state_d4_w8 | 4/4 | 193 | 24.72768 | bounded (49/51, 53/53) | 0 mismatches / 1944 bits |
| fifo_err_flags_d4_w8 | 4/4 | 194 | 25.77744 | proven (52/52, 51/51) | 0 mismatches / 2352 bits |
| fifo_generate_d4_w8 | 4/4 | 171 | 23.88204 | proven (50/50, 49/49) | 0 mismatches / 1960 bits |
| fifo_gray_ptr_d8_w8 | 4/4 | 354 | 49.01796 | proven (98/98, 98/98) | 0 mismatches / 1944 bits |
| fifo_last_d4_w8 | 4/4 | 224 | 29.72862 | proven (58/58, 57/57) | 0 mismatches / 2735 bits |
| fifo_onehot_d4_w8 | 4/4 | 183 | 25.13592 | proven (53/53, 53/53) | 0 mismatches / 1960 bits |
| fifo_pipe_d3_w8 | 4/4 | 105 | 15.07572 | bounded (5/13, 45/45) | 0 mismatches / 1928 bits |
| fifo_ptr_wrap_d8_w8 | 4/4 | 317 | 44.27946 | proven (82/82, 82/82) | 0 mismatches / 1944 bits |
| fifo_regout_d4_w8 | 4/4 | 204 | 28.73718 | proven (52/52, 51/51) | 0 mismatches / 2116 bits |
| fifo_sync_rst_d4_w8 | 4/4 | 195 | 25.90866 | proven (49/49, 49/49) | 0 mismatches / 1944 bits |
| fsm_traffic | 4/4 | 21 | 3.00348 | proven (5/5, 5/5) | 0 mismatches / 588 bits |
| full_adder | 4/4 | 3 | 0.39366 | proven (2/2, 2/2) | 0 mismatches / 392 bits |
| gray_counter4 | 4/4 | 16 | 2.53692 | proven (8/8, 8/8) | 0 mismatches / 784 bits |
| half_adder | 4/4 | 2 | 0.2187 | proven (2/2, 2/2) | 0 mismatches / 392 bits |
| inv | 4/4 | 1 | 0.04374 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| lfsr8 | 4/4 | 26 | 4.03866 | proven (8/8, 8/8) | 0 mismatches / 1568 bits |
| mux2 | 4/4 | 3 | 0.17496 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| mux4 | 4/4 | 6 | 0.42282 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| or2 | 4/4 | 1 | 0.08748 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| parity8 | 4/4 | 7 | 0.91854 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| priority_enc8 | 4/4 | 11 | 0.78732 | proven (4/4, 4/4) | 0 mismatches / 784 bits |
| reg8_en | 4/4 | 32 | 4.31568 | proven (8/8, 8/8) | 0 mismatches / 1560 bits |
| shift_reg8 | 4/4 | 8 | 2.916 | proven (9/9, 9/9) | 0 mismatches / 193 bits |
| skid_buffer | 4/4 | 38 | 5.01552 | proven (10/10, 10/10) | 0 mismatches / 1944 bits |
| sync_fifo_d16_w32 | 4/4 | 2400 | 322.45128 | proven (560/560, 559/559) | 0 mismatches / 1372 bits |
| sync_fifo_d16_w8 | 4/4 | 660 | 88.3548 | proven (152/152, 151/151) | 0 mismatches / 1372 bits |
| sync_fifo_d4_w8 | 4/4 | 192 | 24.5673 | proven (50/50, 49/49) | 0 mismatches / 980 bits |
| sync_fifo_d8_w8 | 4/4 | 350 | 46.90386 | proven (85/85, 84/84) | 0 mismatches / 1176 bits |
| xor2 | 4/4 | 1 | 0.13122 | proven (1/1, 1/1) | 0 mismatches / 196 bits |
| sync_fifo | 4/4 | 2383 | 323.07822 | proven (560/560, 559/559) | 0 mismatches / 1372 bits |
| async_fifo | 4/4 | 757 | 101.85588 | proven (198/198, 198/198) | 0 mismatches / 1960 bits |
| struct_fifo | 4/4 | 876 | 115.24032 | proven (211/211, 211/211) | 0 mismatches / 4856 bits |
| if_fifo | 4/4 | 329 | 43.87122 | proven (89/89, 89/89) | 0 mismatches / 3496 bits |
