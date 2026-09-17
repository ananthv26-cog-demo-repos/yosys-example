# Yosys Boolean-Layer Area Prototype (v1)

`bool_area.py` is the v1 flow: one SystemVerilog block in, a restricted
one-bit Boolean gate graph + ASAP7-mapped cell counts/area out, with every output checked
for functional equivalence against the RTL. Everything is pinned in a versioned profile so
two runs are comparable only when their `profile.sha256` matches.

```
                      ┌── generic_yosys.json ──► boolean_graph.json ──► metrics.json (gates, depth, fanout)
 block.sv ─► slang ─► synth ─► abc -g ─┤
                      └── dfflibmap + abc -liberty (ASAP7 RVT/TT) ─► mapped_yosys.json / mapped_netlist.v ─► cells, area
 equivalence:  RTL ≡ Boolean graph   (equiv_simple + equiv_induct, fallback bounded miter/SAT from reset)
               Boolean graph ≡ ASAP7 netlist
 simulation:   RTL vs ASAP7 netlist, random stimulus, iverilog (sample, not proof)
```

```sh
python3 tools/synth_area/bool_area.py block.sv --top block -o out/block
# [bool_area] OK top=sync_fifo gates=1275 dffs=525 depth=14 cells=2383 area=323.07822 equiv=proven (7.9s)
python3 tools/synth_area/run_suite.py -o out/suite        # the 36-block corpus, hand-count checks, Markdown table
```

## Requirement → implementation

| Requirement | Where | Notes |
|---|---|---|
| Input: synthesizable SV + explicit top | `bool_area.py` CLI (`--top` required, `-I`, `-D`, `--frontend slang\|sv2v\|verilog`) | slang frontend by default; see `SV_SUPPORT.md` for what parses |
| Lower to restricted Boolean gates | `profiles/asap7_rvt_tt_v1.json` → `script.lower` | `synth -flatten` → `dfflegalize` (enable/sync-reset flops → plain DFF + MUX) → `abc -g AND,NAND,OR,NOR,XOR,XNOR,MUX` → `write_json` |
| Graph objects: NOT/AND/NAND/OR/NOR/XOR/XNOR/MUX/DFF, constants, ports | `boolean_graph.py` `build_graph` | one node per gate / DFF / port bit / constant, one edge per (driver, sink, pin); any other cell type → `UnsupportedCell`, nonzero exit |
| DFF as sequential cut | `compute_metrics` | depth resets at DFF inputs; clock edges are reported separately (`clock_fanout`) and excluded from data fanout |
| Metrics: per-gate counts, gate total, DFF count, edges, depth, fanout | `metrics.json` → `summary` (flat shape) and `boolean` (full) | `max_fanout`, `avg_fanout` over data edges only |
| Map to ASAP7, mapped cell counts + Liberty area | `script.map`: `dfflibmap` + `abc -liberty` on the 5 pinned RVT/TT NLDM libs; `stat -liberty -json` | `mapped_cell_area` is the sum of Liberty `area` attributes (um², pre-layout). Cells without an `area` attribute make the run fail rather than under-report |
| Artifacts | `generic_yosys.json`, `mapped_yosys.json`, `mapped_netlist.v`, `boolean_graph.json`, `metrics.json`, `yosys.log`, `synth.ys`, `equiv_*.ys/.log`, `sim/` | all paths listed under `metrics.artifacts` |
| Provenance | `metrics.profile` (name, version, sha256, Liberty files + sha256), `metrics.tools` (yosys version, sv2v), `metrics.frontend` (name, slang args, include dirs, defines), `metrics.sources` (resolved path + sha256 per RTL file given on the command line; files pulled in via `-I` are not hashed), `metrics.timing` per stage, `wall_seconds` | Liberty hashes are verified against the profile on every run (`--no-verify-libs` to skip) |
| Determinism | same profile + sources ⇒ byte-identical `generic_yosys.json`, `mapped_yosys.json`, `mapped_netlist.v`, `boolean_graph.json` | tested in `tests/test_bool_area.py::test_determinism` |
| Behaviour matches RTL | formal: `equiv_rtl_vs_graph`, `equiv_graph_vs_mapped`; simulation: `diff_sim.py` | see below |
| ≥ 20 small blocks incl. hand-countable + FIFOs | `corpus/` (32 blocks) + 4 `examples/` FIFOs, manifest `corpus/suite.json` | 19 blocks carry exact `expect` values (e.g. `inv`: 1 NOT; `parity8`: 7 gates, depth 3; `reg8_en`: 8 DFF + 8 MUX) |
| Nonzero exit on any failure | `metrics.status/stage/errors` + exit code (2 = setup/inputs, 1 = flow) | `metrics.json` is written on failure too |

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

`equiv_status -assert` must report zero unproven cells **and** at least as many `$equiv`
cells as there are output bits (a guard against the two designs silently not being
compared). Result: `equivalence.status = proven`.

**Bounded fallback.** k-induction proves equivalence over *all* states, including ones the
design can never reach from reset. `fifo_shift_d4_w8` (a shift-register FIFO whose 3-bit
`count` can never exceed 4) is functionally identical to its netlist but the two encode
the unreachable `count ∈ 5..7` write-decode differently, so induction leaves 24 cells
unproven at any depth. When that happens `bool_area.py` builds a `miter` of gold and gate,
asserts the reset inputs (as classified for simulation) in cycle 1 and proves the miter
never fires for `--equiv-bmc` (default 10) cycles from a zero initial state — a sound
bounded proof for every input sequence of that length. The run then reports
`equivalence.status = bounded`, records `bmc.depth`, and adds a warning; `--equiv-bmc 0`
turns the fallback off and makes such blocks fail. SAT cost grows steeply with depth (0.4s
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

## Corpus results (`run_suite.py`, profile `asap7_rvt_tt_v1`, one core)

36/36 blocks pass; median 1.4s per block including both proofs and 200 simulated cycles.
The largest block (16×32 FIFO, 525 flops) takes 7.9s, of which 5.8s is the
graph-vs-ASAP7 proof.

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
| sync_fifo_d4_w8 / d8_w8 | proven | 95 / 184 | 39 / 74 | 7 / 11 | 18 / 33 | 192 / 350 | 24.567 / 46.904 |
| sync_fifo_d16_w8 / d16_w32 | proven | 391 / 1261 | 141 / 525 | 14 | 46 / 170 | 660 / 2400 | 88.355 / 322.451 |
| examples: sync_fifo | proven | 1275 | 525 | 14 | 170 | 2383 | 323.078 |
| examples: async_fifo | proven* | 392 | 168 | 12 | 49 | 757 | 101.856 |
| examples: struct_fifo / if_fifo | proven | 392 / 150 | 186 / 71 | 12 / 8 | 89 / 34 | 876 / 329 | 115.240 / 43.871 |

\* single-clock abstraction, see above. Full table with edge counts and per-block times:
`run_suite.py` writes `suite_table.md` / `suite_summary.json`.

Things worth knowing when reading the numbers:

- `mapped_cell_total` can be below `gate_total` (ASAP7 has AOI/OAI/FA/MAJ compound cells)
  or above it (`mux2` → 3 cells: the RVT library has no plain MUX2, so ABC builds one).
- A hand count is exact for the Boolean layer only when there is one optimal structure
  (`inv`, `parity8`, `reg8_en`…). For `full_adder`/`dec2to4` the manifest bounds the count
  instead of fixing it — ABC may legitimately pick NAND/NOR forms.
- Gate counts depend on the frontend and on the profile; never compare runs across
  profiles (the profile hash is in every `metrics.json`, and the profile file says so).

## Adding a block

Drop `corpus/<name>.sv` in, append `{"name", "sources", "top", "expect"?}` to
`corpus/suite.json`, run `run_suite.py`. `expect` values are compared exactly against
`metrics.summary`; `expect_max` gives upper bounds. A block that needs explicit clock/reset
naming for simulation passes them through `run_suite.py ... -- --sim-clock c --sim-reset r:low`
(applies to the whole run) or gets its own entry with `--sim-cycles 0`.

## Files

| | |
|---|---|
| `bool_area.py` | the flow (CLI, stages, metrics, equivalence orchestration) |
| `boolean_graph.py` | `write_json` → Boolean graph + metrics; also a standalone CLI |
| `diff_sim.py` | random differential simulation RTL vs mapped netlist |
| `run_suite.py`, `corpus/suite.json`, `corpus/*.sv` | corpus and its runner |
| `profiles/asap7_rvt_tt_v1.json` | pinned frontend, gate set, DFF policy, pass order, Liberty set + hashes |
| `lib/asap7/` | the 5 ASAP7 RVT/TT NLDM Liberty files (gzip), `PROVENANCE.md`, `LICENSE` |
| `tests/test_bool_area.py` | graph unit tests, hand counts, determinism, broken-netlist detection, BMC fallback, suite runner |
