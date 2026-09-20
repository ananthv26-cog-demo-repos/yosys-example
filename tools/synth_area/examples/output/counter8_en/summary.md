# counter8_en — design-layer summary

- status: **ok** (stage `done`), wall 1.566 s
- profile: `asap7_rvt_tt_v1` v2 (`53f00dd357a3`), 5 pinned Liberty files
- frontend: `slang`; yosys: Yosys 0.69+ (git sha1 150c32b03, Release, GNU /usr/bin/g++-11 11.4.0) [github.com/ananthv26-cog-demo-repos/yosys-example at main]

## Sources

- `<repo>/tools/synth_area/corpus/counter8_en.sv` sha256 `565c2c4fa47f`

## Word level (`word_level.json`)

5 operations: ADD 1, MUX 2, NE 1, REG 1
register bits 8, memory bits 0, input bits 3, output bits 8

## Sequential (`sequential_overlay.json`)

1 registers / 8 bits: $sdffe 1
clocks: `clk` posedge (8 bits)
resets: `rst` sync active-high (8 bits)
enable bits 8, init bits 0

## Boolean (`boolean_graph.json`)

30 gates (AND 7, NAND 1, NOR 15, OR 6, XOR 1), 8 DFF, 84 edges
depth 9, max fanout 8, avg fanout 1.8537

| path class | depth | from | to |
|---|---|---|---|
| reg2reg | 9 | `count[0]` | `count[6]` |
| in2reg | 9 | `en` | `count[6]` |
| reg2out | 0 | `count[0]` | `count[0]` |
| in2out | none | | |

## Mapped (`mapped_cells.json`)

29 cells (8 flops), area **4.53438 um^2** (sequential 2.916)

| cell type | count |
|---|---|
| `DFFHQx4_ASAP7_75t_R` | 8 |
| `AOI21xp33_ASAP7_75t_R` | 5 |
| `NOR3xp33_ASAP7_75t_R` | 4 |
| `OAI21xp33_ASAP7_75t_R` | 3 |
| `AND3x1_ASAP7_75t_R` | 2 |
| `AND4x1_ASAP7_75t_R` | 2 |
| `AOI31xp33_ASAP7_75t_R` | 2 |
| `A2O1A1Ixp33_ASAP7_75t_R` | 1 |
| `INVx1_ASAP7_75t_R` | 1 |
| `NOR2xp33_ASAP7_75t_R` | 1 |

## Verification

- equivalence **proven** — rtl_vs_graph: proven (8/8 pairs, 0.011 s); graph_vs_mapped: proven (8/8 pairs, 0.239 s)
- simulation **match** — 200 cycles, 1568 bits compared, 0 mismatches

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
