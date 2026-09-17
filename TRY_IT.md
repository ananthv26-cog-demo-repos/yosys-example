# Try it: SystemVerilog → Boolean gate graph → area, with proof

Everything in this fork lives under [`tools/synth_area/`](tools/synth_area/); the Yosys sources are unmodified upstream.

**What it does.** Give it one synthesizable SystemVerilog block. In 1–10 s it returns:

1. a graph of the block's logic (`boolean_graph.json`): NOT/AND/NAND/OR/NOR/XOR/XNOR/MUX gates and flip-flops, with the longest flop-to-flop gate chain and fanout;
2. how much silicon that logic needs in an open 7 nm library (`metrics.json` → `mapped_cell_area`, ASAP7 RVT/TT, pre-layout);
3. a SAT proof that the graph and the mapped netlist compute exactly what the original RTL computes (`equivalence.status = proven`), plus a 200-cycle random simulation of RTL vs netlist as a second opinion.

**Why.** A commercial synthesis run is the accurate answer, but it is slow and license-limited, so a designer or an automated search gets a handful of tries a day. This is the fast, license-free proxy that says "this RTL edit made the block bigger/smaller, and it still does the same thing" in seconds, so thousands of candidates can be screened and only the promising ones sent to the slow flow.

## 0. Look without building

A completed run is committed under [`tools/synth_area/examples/output/`](tools/synth_area/examples/output/):

| Directory | What it is |
|---|---|
| `counter8_en/` | Every artifact of one run on an 8-bit counter (30 gates, 8 flops) — small enough to read end to end: `metrics.json`, `boolean_graph.json`, generic and ASAP7 netlists, the exact Yosys scripts, proof logs, the simulation testbench. |
| `sync_fifo/` | The headline block (16×32 FIFO, 1275 gates, 525 flops, 2383 ASAP7 cells, 323.08 µm²): `metrics.json`, ASAP7 netlist, scripts, simulation log. The 1.6 MB graph and the multi-MB Yosys/proof logs are omitted; regenerate with the command in §2. |
| `suite/` | `suite_table.md` / `suite_summary.json`: all 36 corpus blocks, 36/36 pass, median 1.4 s per block. |

Absolute paths in the committed copies were replaced by `<repo>` / `<out>` and the pre-commit hook trimmed trailing whitespace in the logs; nothing else was edited.

Start with [`counter8_en/metrics.json`](tools/synth_area/examples/output/counter8_en/metrics.json): `summary` is the one-screen answer, `boolean` the graph metrics, `mapped` the ASAP7 cells, `equivalence` and `simulation` the correctness evidence, `profile`/`tools`/`sources` the provenance (sha256 of the RTL, the profile and every Liberty file).

## 1. Build (Ubuntu 22.04, ~15 min, ~10 of them compiling Yosys)

```sh
git clone --recurse-submodules https://github.com/ananthv26-cog-demo-repos/yosys-example && cd yosys-example
sudo apt-get install -y build-essential ninja-build bison flex pkg-config libffi-dev libfl-dev \
     libreadline-dev tcl-dev zlib1g-dev libboost-dev iverilog
pip install "cmake>=3.28"                    # Ubuntu's cmake 3.22 is too old for this tree
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release . && cmake --build build -j"$(nproc)"
```

Optional: `sv2v` on `PATH` (https://github.com/zachjs/sv2v/releases, v0.0.13) gives a second SystemVerilog frontend; the simulation step uses it when present and falls back to the raw RTL otherwise. `iverilog` is only needed for the simulation step; without it that step reports `skipped` and the formal proof remains the gate.

## 2. Run one block, then the whole corpus

```sh
python3 tools/synth_area/bool_area.py --top sync_fifo tools/synth_area/examples/sync_fifo.sv -o out/sync_fifo
python3 tools/synth_area/run_suite.py -o out/suite
```

Expected (one core, this machine):

```
[bool_area] OK top=sync_fifo gates=1275 dffs=525 depth=14 cells=2383 area=323.07822 equiv=proven (8.1s)
...
[suite] ok   parity8                gates=7 dffs=0 depth=3 cells=7 area=0.91854 (1.4s)
[suite] ok   fifo_shift_d4_w8       gates=92 dffs=35 depth=10 cells=185 area=23.02182 (2.5s)
[suite] ok   sync_fifo_d16_w32      gates=1261 dffs=525 depth=14 cells=2400 area=322.45128 (7.8s)
[suite] 36/36 blocks passed -> out/suite/suite_summary.json
```

With the Yosys built from this repo your numbers should match the committed ones: on one Yosys build, same RTL + same profile ⇒ byte-identical `boolean_graph.json` and netlists (`tests/test_bool_area.py::test_determinism` checks two runs against each other). A different Yosys/ABC version can legitimately produce a different, equally correct netlist; `metrics.json → tools.yosys_version` and `profile.sha256` record what each run used, so compare like with like.

## 3. What one run produces

| File | What it is |
|---|---|
| `metrics.json` | Everything in one place: gate counts by type, DFF count, edges, `max_depth` (DFF `D` pins are sequential cuts), max/avg fanout (clock edges reported separately and excluded), ASAP7 cell counts and area, equivalence + simulation results, per-stage seconds, provenance. Written on failure too, with `status`, `stage`, `errors`. |
| `boolean_graph.json` | The graph itself: one node per gate/DFF/port bit/constant, one edge per (driver → sink, pin). Any cell outside the allowed set aborts the run (`UnsupportedCell`) instead of being approximated. |
| `generic_yosys.json`, `synth.ys`, `yosys.log` | The generic gate netlist Yosys produced and the exact script that produced it. |
| `mapped_yosys.json`, `mapped_netlist.v`, `stat.txt` | The ASAP7 RVT/TT netlist and Yosys' cell/area table. Five pinned NLDM Liberty files are vendored under `lib/asap7/` with sha256s that are checked on every run (`PROVENANCE.md`, BSD-3 `LICENSE`). |
| `equiv_rtl_vs_graph.{ys,log}`, `equiv_graph_vs_mapped.{ys,log}` | The two proofs. `equiv_cells` = number of compared points (outputs + same-named registers), `unproven` must be 0. |
| `sim/` | Generated testbench (`tb.v`), RTL and netlist as Icarus sees them, `sim.log`: `cycles compared_bits mismatches gate_x_bits`. |

`profiles/asap7_rvt_tt_v1.json` fixes everything that affects the numbers (frontend flags, gate set, DFF legalisation, pass order, Liberty set). Two runs are comparable iff their `metrics.profile.sha256` match.

## 4. Two things to try that show what it is for

**Change RTL, see area move in seconds.** `tools/synth_area/corpus/parity8.sv` is `assign p = ^d;`. Change `^d` to `|d` and rerun:

```
^d :  gates=7 depth=3 cells=7 area=0.91854 equiv=proven (1.4s)   # 7 XOR → 7 XOR2 cells
|d :  gates=7 depth=3 cells=3 area=0.23328 equiv=proven (1.4s)   # 7 OR  → 3 cells (ASAP7 has wide OR cells)
```

Same Boolean gate count, 4× less silicon — the graph metric and the mapped area disagree on purpose, and `equiv=proven` holds against the *edited* RTL. Revert and rerun: `boolean_graph.json` is byte-identical to the first run.

**Make it fail.**

- `--frontend verilog` on `examples/fifo_pkg.sv examples/struct_fifo.sv` (native Yosys chokes on the package's `function automatic int unsigned` with a ternary) → exit 1, `metrics.json` has `status: failed, stage: parse` and the parser error.
- `--equiv-bmc 0` on `corpus/fifo_shift_d4_w8.sv` → exit 1, `stage: equivalence`, `equivalence.status: failed`. This block is only provable *bounded* (10 cycles from reset) because RTL and netlist encode the unreachable states `count ∈ 5..7` differently, so k-induction fails; the default run says so (`equivalence.status = bounded`, warning in `metrics.json`) rather than hiding it.

## 5. Map of the code

| | |
|---|---|
| `synth_area.py` | Layer 1: SystemVerilog → Yosys → JSON gate/area report; frontends `slang` (default), `sv2v`, native. |
| `SV_SUPPORT.md`, `sv_probes/`, `sv_matrix.py` | Which SystemVerilog constructs each frontend accepts (slang 33/34, sv2v 30/34, native 16/34). |
| `lib/asap7/`, `profiles/asap7_rvt_tt_v1.json` | Pinned library and the versioned synthesis profile. |
| `boolean_graph.py` | `write_json` → Boolean gate graph + metrics. |
| `diff_sim.py` | Random differential simulation RTL vs netlist. |
| `bool_area.py` | The flow: lower → graph → map → prove → simulate → `metrics.json`. |
| `corpus/`, `run_suite.py`, `BOOLEAN_LAYER.md` | 36 blocks (19 with hand-counted expected metrics), the runner, and the design/results write-up. |
| `slurm_synth_area.sh`, `slurm_sweep.sh` | `sbatch` wrappers for running many blocks in parallel (fall back to local execution without `sbatch`). |
| `tests/` | `python3 tools/synth_area/tests/test_<name>.py` — all need `build/yosys`; ~1 min total. |

## 6. Not done / not claimed

- **No correlation with a commercial flow.** Until the same blocks are run through one with a matching library, the ASAP7 numbers are directional (does the edit make it bigger or smaller, by roughly how much), not calibrated.
- **Area only.** No timing, power, placement; no graph rewriting or optimisation yet — this is the measurement half of the loop.
- **Memories become flops.** Yosys lowers the FIFO storage to DFFs (hence 512 `DFFHQx4` in `sync_fifo`); a real flow would use a macro.
- **Multi-clock blocks** (`async_fifo`) are proven under Yosys' single-clock `async2sync` abstraction: logic preserved, CDC timing not modelled.
- **Simulation is a sample**, 200 cycles with one seed; the SAT proof is the correctness gate.
- **Slurm wrappers** have not been run on a real cluster.
- Timings above are one core, one machine; no parallel-throughput measurement yet.
