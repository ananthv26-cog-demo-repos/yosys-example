# sync_fifo — design-layer summary

- status: **ok** (stage `done`), wall 10.499 s
- profile: `asap7_rvt_tt_v1` v2 (`53f00dd357a3`), 5 pinned Liberty files
- frontend: `slang`; yosys: Yosys 0.69+ (git sha1 150c32b03, Release, GNU /usr/bin/g++-11 11.4.0) [github.com/ananthv26-cog-demo-repos/yosys-example at main]

## Sources

- `<repo>/tools/synth_area/examples/sync_fifo.sv` sha256 `2c0460fe1659`

## Word level (`word_level.json`)

26 operations: ADD 3, EQ 4, LOGIC_AND 2, LOGIC_NOT 4, LOGIC_OR 1, MEMRD 1, MEMWR 1, MUX 4, NE 1, PMUX 1, REG 3, SUB 1
register bits 13, memory bits 512, input bits 36, output bits 39

## Sequential (`sequential_overlay.json`)

3 registers / 13 bits: $adffe 3
clocks: `clk_i` posedge (13 bits)
resets: `rst_ni` async active-low (13 bits)
enable bits 13, init bits 0

## Boolean (`boolean_graph.json`)

1275 gates (AND 120, MUX 899, NAND 194, NOR 16, NOT 11, OR 21, XNOR 9, XOR 5), 525 DFF, 4540 edges
depth 14, max fanout 170, avg fanout 2.1797

## Mapped (`mapped_cells.json`)

2383 cells (525 flops), area **323.07822 um^2** (sequential 191.55204)

| cell type | count |
|---|---|
| `NAND2xp33_ASAP7_75t_R` | 561 |
| `OAI21xp33_ASAP7_75t_R` | 558 |
| `DFFHQx4_ASAP7_75t_R` | 512 |
| `AOI21xp33_ASAP7_75t_R` | 134 |
| `INVx1_ASAP7_75t_R` | 111 |
| `NOR2xp33_ASAP7_75t_R` | 73 |
| `A2O1A1Ixp33_ASAP7_75t_R` | 70 |
| `AO21x1_ASAP7_75t_R` | 66 |
| `O2A1O1Ixp33_ASAP7_75t_R` | 42 |
| `AND2x2_ASAP7_75t_R` | 33 |
| `OA21x2_ASAP7_75t_R` | 33 |
| `AOI211xp5_ASAP7_75t_R` | 26 |
| `OAI211xp5_ASAP7_75t_R` | 26 |
| `OR2x2_ASAP7_75t_R` | 22 |
| `OAI311xp33_ASAP7_75t_R` | 15 |
| `DFFASRHQNx1_ASAP7_75t_R` | 13 |
| `OA22x2_ASAP7_75t_R` | 12 |
| `AOI22xp33_ASAP7_75t_R` | 11 |
| `OAI221xp5_ASAP7_75t_R` | 11 |
| `XNOR2xp5_ASAP7_75t_R` | 10 |
| `NAND3xp33_ASAP7_75t_R` | 6 |
| `OAI22xp33_ASAP7_75t_R` | 5 |
| `A2O1A1O1Ixp25_ASAP7_75t_R` | 4 |
| `AOI31xp33_ASAP7_75t_R` | 4 |
| `NOR3xp33_ASAP7_75t_R` | 4 |
| `NOR5xp2_ASAP7_75t_R` | 4 |
| `AOI311xp33_ASAP7_75t_R` | 3 |
| `OA211x2_ASAP7_75t_R` | 3 |
| `OAI32xp33_ASAP7_75t_R` | 2 |
| `AO221x1_ASAP7_75t_R` | 1 |
| `AO32x1_ASAP7_75t_R` | 1 |
| `AOI221xp5_ASAP7_75t_R` | 1 |
| `AOI321xp33_ASAP7_75t_R` | 1 |
| `AOI32xp33_ASAP7_75t_R` | 1 |
| `MAJIxp5_ASAP7_75t_R` | 1 |
| `NAND4xp25_ASAP7_75t_R` | 1 |
| `NAND5xp2_ASAP7_75t_R` | 1 |
| `NOR4xp25_ASAP7_75t_R` | 1 |

## Verification

- equivalence **proven** — rtl_vs_graph: proven (560/560 pairs, 0.352 s); graph_vs_mapped: proven (559/559 pairs, 7.13 s)
- simulation **match** — 200 cycles, 1372 bits compared, 0 mismatches

## Warnings

- ABC: Warning: The network is combinational (run "fraig" or "fraig_sweep").
- ABC: Warning: Detected 2 multi-output cells (for example, "FAx1_ASAP7_75t_R").

## Artifacts

- `boolean_graph.json`
- `generic_yosys.json`
- `mapped_cells.json`
- `mapped_netlist.v`
- `mapped_yosys.json`
- `metrics.json`
- `run_manifest.json`
- `sequential_overlay.json`
- `summary.md`
- `word.log`
- `word.ys`
- `word_level.json`
- `word_yosys.json`
- `yosys.log`
