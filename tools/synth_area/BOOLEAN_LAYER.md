# Yosys Design-Layer Reporter (v2)

`bool_area.py` takes one SystemVerilog block and reports the same design at four layers —
word-level operations, registers, one-bit Boolean gates, ASAP7 cells — one JSON file each,
with the gate and cell layers proven equivalent to the RTL. Everything is pinned in a
versioned profile so two runs are comparable only when their `profile.sha256` matches.

```
 block.sv ─► slang ─► proc; flatten; opt_dff ─► word_yosys.json ─┬► word_level.json         (layer 1: ADD/MUX/EQ/MEMRD…, widths, src lines)
                                    │                       └► sequential_overlay.json (layer 2: per-register clk/reset/enable/init)
                                    └► synth ─► abc -g ─┬─► generic_yosys.json ─► boolean_graph.json (layer 3: gates, DFFs, depth, fanout)
                                                       └─► dfflibmap + abc -liberty (ASAP7 RVT/TT) ─► mapped_yosys.json / mapped_netlist.v
                                                                                                       └► mapped_cells.json (layer 4: cell, area, function, pin→net)
 metrics.json  per-layer totals + verdicts      summary.md  one page      run_manifest.json  argv, versions, sha256 of every in/output
 equivalence:  RTL ≡ Boolean graph   (equiv_simple + equiv_induct, fallback bounded miter/SAT from reset)
               Boolean graph ≡ ASAP7 netlist
 simulation:   RTL vs ASAP7 netlist, random stimulus, iverilog (sample, not proof)
```

```sh
python3 tools/synth_area/bool_area.py block.sv --top block -o out/block
# [bool_area] OK top=sync_fifo gates=1275 dffs=525 depth=14 cells=2383 area=323.07822 equiv=proven (10.5s)
python3 tools/synth_area/run_suite.py -o out/suite -j0     # the 49-block corpus, hand-count checks, evidence rollup
```

## The four layers

| Layer | File | Produced from | One record |
|---|---|---|---|
| 1 word-level | `word_level.json` | the word-level checkpoint: RTLIL after `proc; flatten; opt_clean; opt_dff` (`word.ys`, before `synth`, so operators are still multi-bit) via `write_json` → `word_level.py` | `{kind: "ADD", width: 8, signed: {A: false, B: false}, inputs: {A: {width: 8, signal: "count"}, B: {… "8'b00000001"}}, outputs: {Y: …}, src: "counter8_en.sv:5.31-5.43"}`; memories get `{name, width, size, write_ports, read_ports}`; any `$`-cell not in the kind table → `UnsupportedCell` |
| 2 sequential | `sequential_overlay.json` | the register cells of the same checkpoint (`word_yosys.json`) via `sequential_overlay.py` | `{width: 8, yosys_type: "$sdffe", clock: {signal: "clk", edge: "posedge"}, reset: {kind: "sync", signal: "rst", active: "high", value: "8'b0…"}, enable: {…}, init: null, bits: [{q: "count[0]", d: …}, …]}`; reset kinds `sync`, `async`, `async_set`, `async_clear`; `bits[].q` use the same net-name choice as the `boolean_graph.json` DFF node labels (shared `alias_rank`), so the two files join by register name |
| 3 Boolean | `boolean_graph.json` | `generic_yosys.json` via `boolean_graph.py` | one node per gate/DFF/port bit/constant with `type`, `label`, `src`; one edge per (driver, sink, pin); per-type counts, `max_depth` (cut at DFF `D`), `depth_by_path` (the same depth split into `reg2reg` / `in2reg` / `reg2out` / `in2out`, each with its endpoints) and fanout go to `metrics.json → boolean` |
| 4 mapped | `mapped_cells.json` | `mapped_yosys.json` + the pinned Liberty files via `mapped_cells.py` | `{type: "AOI21xp33_ASAP7_75t_R", area: 0.08748, pins: {A1: {direction: "input", net: …}, A2: …, B: …, Y: {direction: "output", net: …, function: "(!A1 * !B) + (!A2 * !B)"}}}`; `summary`: `cells`, `area`, `by_type`, `pins` |

`metrics.json` carries each layer's totals under `word_level`, `sequential`, `boolean`,
`mapped`; `summary.md` renders them for a reader; `run_manifest.json` lists every layer's
`schema_version` (`layers`) and the sha256 of every artifact, so a report can be checked
against the run that made it. Layers 1 and 2 are descriptive: they are read off the
checkpoint between RTL and gates and are not proven separately; layers 3 and 4 are what the
equivalence checks below compare to the RTL.

## Requirement → implementation

| Requirement | Where | Notes |
|---|---|---|
| Input: synthesizable SV + explicit top | `bool_area.py` CLI (`--top` required, `-I`, `-D`, `--frontend slang\|sv2v\|verilog`) | slang frontend by default; see `SV_SUPPORT.md` for what parses |
| Lower to restricted Boolean gates | `profiles/asap7_rvt_tt_v1.json` → `script.lower` | `synth -flatten` → `dfflegalize` (enable/sync-reset flops → plain DFF + MUX) → `abc -g AND,NAND,OR,NOR,XOR,XNOR,MUX` → `write_json` |
| Graph objects: NOT/AND/NAND/OR/NOR/XOR/XNOR/MUX/DFF, constants, ports | `boolean_graph.py` `build_graph` | one node per gate / DFF / port bit / constant, one edge per (driver, sink, pin); any other cell type → `UnsupportedCell`, nonzero exit |
| DFF as sequential cut | `compute_metrics` | depth resets at DFF inputs; clock edges are reported separately (`clock_fanout`) and excluded from data fanout |
| Depth per path class | `path_class_depths` | `max_depth` is the longest chain over all four classes; `depth_by_path` reports each class on its own: `reg2reg` (DFF Q → DFF D, internal), `in2reg` (input port → DFF D) and `reg2out` (DFF Q → output port) — the depths a neighbouring block sees at the boundary — and `in2out` (input → output, unregistered through-path). Each carries the `from`/`to` endpoint names of its longest path, or is null when the block has no path of that class (e.g. `in2out` on a block with registered outputs). The flat `summary` has them as `depth_reg2reg` … `depth_in2out` |
| Metrics: per-gate counts, gate total, DFF count, edges, depth, fanout | `metrics.json` → `summary` (flat shape) and `boolean` (full) | `max_fanout`, `avg_fanout` over data edges only |
| Map to ASAP7, mapped cell counts + Liberty area | `script.map`: `dfflibmap` + `abc -liberty` on the 5 pinned RVT/TT NLDM libs; `stat -liberty -json` | `mapped_cell_area` is the sum of Liberty `area` attributes (um², pre-layout). Cells without an `area` attribute make the run fail rather than under-report |
| Word-level / sequential / mapped-cell reports | `word_level.py`, `sequential_overlay.py`, `mapped_cells.py` | see "The four layers" above |
| Artifacts | `word_level.json`, `sequential_overlay.json`, `boolean_graph.json`, `mapped_cells.json`, `metrics.json`, `summary.md`, `run_manifest.json`, `generic_yosys.json`, `mapped_yosys.json`, `mapped_netlist.v`, `word.ys/.log`, `synth.ys`, `yosys.log`, `equiv_*.ys/.log`, `sim/` | all paths listed under `metrics.artifacts`; the manifest hashes each one. A report that cannot be written marks the run `failed`/`artifacts` and the remaining reports are still written and say so |
| Provenance | `metrics.profile` (name, version, sha256, Liberty files + sha256), `metrics.tools` (yosys version, sv2v), `metrics.frontend` (name, slang args, include dirs, defines), `metrics.sources` (resolved path + sha256 per RTL file given on the command line; files pulled in via `-I` are not hashed), `metrics.timing` per stage, `wall_seconds` | Liberty hashes are verified against the profile on every run (`--no-verify-libs` to skip) |
| Determinism | same profile + sources ⇒ byte-identical `generic_yosys.json`, `mapped_yosys.json`, `mapped_netlist.v`, `boolean_graph.json` | tested in `tests/test_bool_area.py::test_determinism` |
| Behaviour matches RTL | formal: `equiv_rtl_vs_graph`, `equiv_graph_vs_mapped`; simulation: `diff_sim.py` | see below |
| ≥ 20 small blocks incl. hand-countable + ≥ 20 FIFOs | `corpus/` (45 blocks) + 4 `examples/` FIFOs, manifest `corpus/suite.json` | 32 blocks carry `expect` / `expect_max` values (e.g. `inv`: 1 NOT; `parity8`: 7 gates, depth 3; `reg8_en`: 8 DFF + 8 MUX; every new FIFO: its hand-counted flop total). 24 blocks are FIFO variants — pointer, shift, one-hot, Gray, FWFT, registered-output, bypass, elastic pipeline, almost-full flags, sticky error flags, `last` sideband, enum state, generate storage, sync/async reset, flush, struct/interface/package ports, dual clock |
| Nonzero exit on any failure | `metrics.status/stage/errors` + exit code (2 = setup/inputs, 1 = flow) | `metrics.json` is written on failure too, for every run that gets past argument parsing; a usage error (unknown flag, malformed `--top`, `--equiv-bmc 1`) exits 2 with argparse's message before an output directory exists |

### Out of scope in v1
No graph rewriting / optimisation, no timing or power, no placement, no correlation against a
commercial synthesis flow (needs its reports + the process `.lib`), no latches / multi-port memories
beyond what `synth` lowers to flops, no commercial equivalence checker.

## Equivalence: what is actually proven

Two checks, both with Yosys' SAT-based `equiv_*` passes on the flattened designs:

1. **RTL vs Boolean graph** — `read_slang` + `proc; flatten; memory_map` (gold) against
   `generic_yosys.json` (gate). Outputs and same-named registers are paired
   (`equiv_make`), then `equiv_simple -seq 5` and `equiv_induct -seq 5` prove every pair.
2. **Boolean graph vs ASAP7 netlist** — `generic_yosys.json` against `mapped_yosys.json`
   with the ASAP7 cells expanded from their Liberty functions. Flop `Q` wires carry a
   `keep` attribute through mapping so registers pair by name; that costs a few inverters
   (a `QN`-only cell followed by an `INV`) and is the price of a wire-for-wire proof.
   Because every register is paired, `-seq 1` induction is normally enough here, so it is
   tried first (`--equiv-mapped-seq`, default 1) and only cells it leaves unproven are
   retried at `--equiv-seq`; `checks.graph_vs_mapped.seq` records the depth that closed the
   proof and a kept `equiv_graph_vs_mapped_seq1.{ys,log}` marks a retry.

`equiv_status -assert` must report zero unproven cells **and** at least as many `$equiv`
cells as there are output bits (a guard against the two designs silently not being
compared). Result: `equivalence.status = proven`.

**Bounded fallback.** k-induction proves equivalence over *all* states, including ones the
design can never reach from reset. `fifo_shift_d4_w8` (a shift-register FIFO whose 3-bit
`count` can never exceed 4) is functionally identical to its netlist but the two encode
the unreachable `count ∈ 5..7` write-decode differently, so induction leaves 24 cells
unproven at any depth. When that happens `bool_area.py` builds a `miter` of gold and gate,
starts both from an *undefined* state, asserts the reset inputs (as classified for
simulation) in cycle 1 and proves the miter never fires in cycles 2..`--equiv-bmc` (default
10) — a bounded proof for every input sequence of that length and every power-up state.
Only registers the reset initialises become defined; a gold output that is still `x` is
unspecified by the RTL and is not compared, while every defined gold output must be matched
by a defined, equal gate output (forcing all registers to zero instead would silently
exclude legal non-zero power-up states). The run then reports
`equivalence.status = bounded`, records `bmc.depth`, and adds a warning; `--equiv-bmc 0`
turns the fallback off and makes such blocks fail; the smallest accepted depth is 2 (one
compared cycle after reset). SAT cost grows steeply with depth (0.4s
at 10, ~30s at 20 for this block).

**Multi-clock designs.** Both checks run `async2sync` and treat all clocks as one; that is
the standard Yosys abstraction and is sound for the combinational logic + register
structure, but it does not model clock-domain crossing timing. `async_fifo` therefore
proves "each domain's logic is preserved", not "the CDC is safe".

## Simulation (`diff_sim.py`)

A generated testbench drives random stimulus (fixed `--seed`) into the RTL (converted with
`sv2v` for Icarus) and the ASAP7 netlist (cells modelled from the Liberty functions via
Yosys `write_verilog`), toggles every clock each cycle, holds resets for 4 cycles, and
compares every output bit the RTL drives to a known value. Clocks/resets are inferred from
names (`clk`, `clock` / `rst`, `reset`; `_n`/`_ni`/`_b`/`_l` suffix ⇒ active-low; `*_en`,
`*_sel`, `*_valid` … are never clocks) or given explicitly with `--sim-clock PORT` and
`--sim-reset PORT[:low]` (exactly that grammar; explicit names must be input ports of the top, otherwise the run
fails at the `inputs` stage); the choice is recorded in `metrics.simulation`. A netlist bit
that is X/Z where the RTL is 0/1 is counted in `simulation.gate_x_bits` and raised as a
warning, not a mismatch: with uninitialised flops and Liberty-derived cell models it is
X-pessimism, and the formal check above already proves those bits functionally equal. This
is a sample, not a proof — it is kept because it also exercises the Liberty cell models,
which the formal check does not. `--sim-cycles 0` disables it.

**Scheduling.** The two proofs and the simulation only need the mapped netlist, so once
synthesis is done `bool_area.py` runs them as three concurrent processes (`--check-jobs`,
default 3; `1` runs them one after another). Each writes only its own files
(`equiv_rtl_vs_graph*`, `equiv_graph_vs_mapped*`, `sim/`) and the results are merged in a
fixed order, so `metrics.json` is byte-identical apart from the timings and an equivalence
failure is always reported ahead of a simulation mismatch. `timing.checks_seconds` is the
wall time of that phase; the win is bounded by the slowest of the three (on the 16×32
FIFO 3.2 s → 2.4 s, on the 64×32 one 31.6 s → 28.0 s, both dominated by the graph-vs-ASAP7
proof). A `run_suite.py -j8` run already keeps every core busy, so there the gain is
smaller (49 blocks 17.1 s → 16.1 s) but it does not slow down either.

## Corpus results (`run_suite.py`, profile `asap7_rvt_tt_v1` v2, one core)

49/49 blocks pass, 46 `proven` and 3 `bounded`, all 49 `match` in simulation. Median
2.4 s per block including the four layer reports, both proofs and 200 simulated cycles;
the largest block (16×32 FIFO, 525 flops) takes 10.4 s, most of it the graph-vs-ASAP7 proof.

| block | equiv | gates | dff | depth | max fanout | ASAP7 cells | area (um²) |
|---|---|---|---|---|---|---|---|
| inv / and2 / or2 / xor2 | proven | 1 | 0 | 1 | 1 | 1 | 0.044–0.131 |
| mux2 | proven | 1 | 0 | 1 | 1 | 3 | 0.175 |
| half_adder | proven | 2 | 0 | 1 | 2 | 2 | 0.219 |
| full_adder | proven | 5 | 0 | 3 | 2 | 3 | 0.394 |
| parity8 | proven | 7 | 0 | 3 | 1 | 7 | 0.919 |
| mux4 | proven | 3 | 0 | 2 | 2 | 6 | 0.423 |
| dec2to4 | proven | 5 | 0 | 2 | 4 | 5 | 0.335 |
| dff / dff_async_rst | proven | 0 | 1 | 0 | 1 | 1 / 2 | 0.365 / 0.423 |
| dff_sync_rst | proven | 2 | 1 | 2 | 1 | 3 | 0.467 |
| shift_reg8 | proven | 0 | 8 | 0 | 1 | 8 | 2.916 |
| reg8_en | proven | 8 | 8 | 1 | 8 | 32 | 4.316 |
| counter4 / counter8_en | proven | 11 / 30 | 4 / 8 | 4 / 9 | 4 / 8 | 13 / 29 | 2.187 / 4.534 |
| gray_counter4 / lfsr8 | proven | 17 / 14 | 4 / 8 | 4 | 5 / 8 | 16 / 26 | 2.537 / 4.039 |
| adder8 / comparator8 | proven | 37 / 49 | 0 | 15 / 11 | 2 | 32 / 26 | 3.222 / 1.866 |
| priority_enc8 / alu4 | proven | 20 / 95 | 0 | 5 / 13 | 3 / 7 | 11 / 60 | 0.787 / 5.030 |
| fsm_traffic / skid_buffer | proven | 25 / 14 | 5 / 9 | 5 | 5 / 8 | 21 / 38 | 3.003 / 5.016 |
| fifo_d2_w4 | proven | 33 | 12 | 6 | 5 | 64 | 8.019 |
| fifo_fwft_d4_w8 | proven | 94 | 39 | 8 | 18 | 184 | 24.567 |
| fifo_shift_d4_w8 | bounded (10) | 92 | 35 | 10 | 25 | 185 | 23.022 |
| fifo_pipe_d3_w8 | bounded (10) | 40 | 27 | 6 | 8 | 105 | 15.076 |
| fifo_enum_state_d4_w8 | bounded (10) | 104 | 39 | 10 | 19 | 193 | 24.728 |
| fifo_sync_rst / clr / bypass / generate (d4_w8) | proven | 104 / 109 / 104 / 140 | 41 / 39 / 39 / 39 | 9 / 11 / 7 / 8 | 16 / 18 / 18 / 9 | 195 / 210 / 223 / 171 | 25.909 / 25.530 / 27.702 / 23.882 |
| fifo_regout / onehot / err_flags / last (d4_w8) | proven | 103 / 121 / 103 / 115 | 48 / 43 / 41 / 46 | 8 / 8 / 8 / 9 | 18 / 10 / 18 / 20 | 204 / 183 / 194 / 224 | 28.737 / 25.136 / 25.777 / 29.729 |
| fifo_ptr_wrap / gray_ptr / almost_flags (d8_w8) | proven | 230 / 179 / 179 | 72 / 78 / 74 | 9 / 8 / 10 | 20 / 31 / 31 | 317 / 354 / 357 | 44.279 / 49.018 / 47.239 |
| sync_fifo_d4_w8 / d8_w8 | proven | 95 / 184 | 39 / 74 | 7 / 11 | 18 / 33 | 192 / 350 | 24.567 / 46.904 |
| sync_fifo_d16_w8 / d16_w32 | proven | 391 / 1261 | 141 / 525 | 14 | 46 / 170 | 660 / 2400 | 88.355 / 322.451 |
| examples: sync_fifo | proven | 1275 | 525 | 14 | 170 | 2383 | 323.078 |
| examples: async_fifo | proven* | 392 | 168 | 12 | 49 | 757 | 101.856 |
| examples: struct_fifo / if_fifo | proven | 392 / 150 | 186 / 71 | 12 / 8 | 89 / 34 | 876 / 329 | 115.240 / 43.871 |

\* single-clock abstraction, see above. Full table with edge counts and per-block times:
`run_suite.py` writes `suite_table.md` / `suite_summary.json`; the committed copy is
`examples/output/suite/suite_table.md`.

`suite_evidence.py` turns the same run into `EVIDENCE.md` / `suite_evidence.json`
(`examples/output/suite/`): every claim this tool makes — all four layers present, ASAP7
area summed, nothing silently dropped, both proofs clean, simulation clean, `src` coverage
per layer, and layer files byte-identical to a `--baseline` run — scored against the
artifacts the run actually wrote, with the sha256 of all 196 layer files. Blocks run one
per process, `-j N` of them at a time (`-j0` = one per core): 27 s on 8 cores vs 78 s
sequential, with results still reported in manifest order.

The two new `bounded` blocks fail k-induction for the same reason as `fifo_shift_d4_w8`:
state the RTL never reaches. `fifo_pipe_d3_w8` has un-reset data registers that only matter
while their stage's `full` flag is set; `fifo_enum_state_d4_w8` has a 2-bit enum that
`fsm` re-encodes, so RTL and netlist disagree on the unused encoding. The bounded proof from
reset covers the reachable states.

Things worth knowing when reading the numbers:

- `mapped_cell_total` can be below `gate_total` (ASAP7 has AOI/OAI/FA/MAJ compound cells)
  or above it (`mux2` → 3 cells: the RVT library has no plain MUX2, so ABC builds one).
- A hand count is exact for the Boolean layer only when there is one optimal structure
  (`inv`, `parity8`, `reg8_en`…). For `full_adder`/`dec2to4` the manifest bounds the count
  instead of fixing it — ABC may legitimately pick NAND/NOR forms.
- Flop counts are exact but not always the naive RTL sum: `memory_dff` can absorb a
  registered read pointer into the memory (`fifo_sync_rst_d4_w8`: +2), `opt_merge` folds
  identical bits (`fifo_gray_ptr_d8_w8`: Gray MSB ≡ binary MSB, −2), `fsm` re-encodes
  enums. Each FIFO's header comment gives the derivation; `sequential_overlay.json` shows
  which registers survived and what drives them.
- Gate counts depend on the frontend and on the profile; never compare runs across
  profiles (the profile hash is in every `metrics.json`, and the profile file says so).
- A profile is build configuration, like a Makefile: its `script.lower` / `script.map`
  lines are Yosys commands run as written, so only run profiles you would run as a script.
- The output directory belongs to the run: every file named in `ARTIFACTS`, `equiv_*` and
  `sim/` under `-o` is deleted before each run, so give each run its own directory.

## Adding a block

Drop `corpus/<name>.sv` in, append `{"name", "sources", "top", "expect"?}` to
`corpus/suite.json`, run `run_suite.py`. `expect` values are compared exactly against
`metrics.summary`; `expect_max` gives upper bounds. A block that needs explicit clock/reset
naming for simulation passes them through `run_suite.py ... -- --sim-clock c --sim-reset r:low`
(applies to the whole run) or carries them in its manifest entry as
`"args": ["--sim-clock", "c", "--sim-reset", "r:low"]` (per block, placed before the run-wide
extras so a run-wide `--sim-cycles 0` still wins).

## Files

| | |
|---|---|
| `bool_area.py` | the flow (CLI, stages, metrics, `summary.md`, `run_manifest.json`, equivalence orchestration) |
| `word_level.py` | RTLIL-after-`proc` `write_json` → `word_level.json`; also a standalone CLI |
| `sequential_overlay.py` | flop cells → `sequential_overlay.json`; also a standalone CLI |
| `boolean_graph.py` | `write_json` → Boolean graph + metrics; also a standalone CLI |
| `mapped_cells.py` | Liberty parser + mapped netlist → `mapped_cells.json`; also a standalone CLI |
| `diff_sim.py` | random differential simulation RTL vs mapped netlist |
| `run_suite.py`, `corpus/suite.json`, `corpus/*.sv` | corpus and its runner |
| `suite_evidence.py` | suite run → `EVIDENCE.md` / `suite_evidence.json` (criteria, layer hashes, determinism vs a baseline) |
| `profiles/asap7_rvt_tt_v1.json` | pinned frontend, gate set, DFF policy, pass order, Liberty set + hashes |
| `lib/asap7/` | the 5 ASAP7 RVT/TT NLDM Liberty files (gzip), `PROVENANCE.md`, `LICENSE` |
| `tests/` | `test_word_level.py`, `test_sequential_overlay.py`, `test_boolean_graph.py`, `test_mapped_cells.py` (one per layer, hand-built netlists + real runs); `test_bool_area.py` (flow, determinism, broken-netlist detection, BMC fallback); `test_suite.py`, `test_diff_sim.py`, `test_synth_area.py` |
