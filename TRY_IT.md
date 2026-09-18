# Try it: SystemVerilog → word-level ops → Boolean gate graph → ASAP7 cells, with proof

Everything in this fork lives under [`tools/synth_area/`](tools/synth_area/); the Yosys sources are unmodified upstream.

**What it does.** Give it one synthesizable SystemVerilog block. In 1–10 s it returns the same design at four layers, one JSON file each, plus the evidence that they agree:

1. `word_level.json` — the multi-bit operations the RTL describes (`ADD width=8`, `MUX`, `NE`, `REG`, memories), with operand widths, signedness and the source line of each;
2. `sequential_overlay.json` — every register: clock and edge, reset kind/polarity/value, enable, init, the RTL name of each bit;
3. `boolean_graph.json` — the logic as NOT/AND/NAND/OR/NOR/XOR/XNOR/MUX gates and flip-flops, with the longest flop-to-flop gate chain (depth) and fanout;
4. `mapped_cells.json` — the ASAP7 (open 7 nm, RVT/TT) cells that logic becomes, each with its Liberty area, function and pin-to-net connections; total area in `metrics.json → mapped_cell_area`;
5. a SAT proof that the gate graph and the mapped netlist compute exactly what the RTL computes (`equivalence.status = proven`), plus a 200-cycle random simulation of RTL vs netlist as a second opinion.

`summary.md` is the one-page human view of all of it; `run_manifest.json` records what produced it (argv, tool versions, sha256 of every input and output).

**Why.** A commercial synthesis run is the accurate answer, but it is slow and license-limited, so a designer or an automated search gets a handful of tries a day. This is the fast, license-free proxy that says "this RTL edit made the block bigger/smaller, and it still does the same thing" in seconds, so thousands of candidates can be screened and only the promising ones sent to the slow flow.

## 0. Look without building

A completed run is committed under [`tools/synth_area/examples/output/`](tools/synth_area/examples/output/):

| Directory | What it is |
|---|---|
| `counter8_en/` | Every artifact of one run on an 8-bit counter (5 word-level ops, 1 register × 8 bits, 30 gates, 29 ASAP7 cells) — small enough to read end to end: the four layer files, `summary.md`, `run_manifest.json`, `metrics.json`, generic and ASAP7 netlists, the exact Yosys scripts, proof logs, the simulation testbench. |
| `sync_fifo/` | The headline block (16×32 FIFO, 1275 gates, 525 flops, 2383 ASAP7 cells, 323.08 µm²): `summary.md`, `run_manifest.json`, `metrics.json`, `word_level.json`, `sequential_overlay.json`, ASAP7 netlist, scripts, simulation log. The ~1 MB graph / mapped-cell JSONs and the multi-MB logs are omitted; regenerate with the command in §2. |
| `suite/` | The whole corpus: [`EVIDENCE.md`](tools/synth_area/examples/output/suite/EVIDENCE.md) (one page: each goal of the tool against what the run measured), `suite_evidence.json` (the same machine-readable, with every block's layer sha256s), `suite_table.md` / `suite_summary.json` (per-block metrics). All 49 blocks (24 of them FIFO variants), 49/49 pass, 9/9 criteria. |
| `stress/` (RTL only) | Two oversized FIFO wrappers, `sync_fifo_d64_w32.sv` and `sync_fifo_d1024_w8.sv`, kept out of the corpus because of their proof time — see the scaling table in §2. |

Paths in the committed copies (absolute, and the work-dir-relative `src` attributes in the JSON) were replaced by `<repo>` / `<out>` and the pre-commit hook trimmed trailing whitespace in the logs; nothing else was edited. That edit is why the sha256s in the committed `run_manifest.json` do not match the committed files next to it — they are the hashes of the unedited originals.

Start with [`suite/EVIDENCE.md`](tools/synth_area/examples/output/suite/EVIDENCE.md) for the corpus-wide answer, then [`counter8_en/summary.md`](tools/synth_area/examples/output/counter8_en/summary.md), then [`metrics.json`](tools/synth_area/examples/output/counter8_en/metrics.json): `summary` is the one-screen answer, `word_level` / `sequential` / `boolean` / `mapped` the per-layer totals, `equivalence` and `simulation` the correctness evidence, `profile`/`tools`/`sources` the provenance (sha256 of the RTL, the profile and every Liberty file).

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
python3 tools/synth_area/run_suite.py -o out/suite -j0      # -j0 = one block per core
```

Expected (one core, this machine):

```
[bool_area] OK top=sync_fifo gates=1275 dffs=525 depth=14 cells=2383 area=323.07822 equiv=proven (10.5s)
...
[suite] ok   parity8                gates=7 dffs=0 depth=3 cells=7 area=0.91854 (1.4s)
[suite] ok   fifo_gray_ptr_d8_w8    gates=179 dffs=78 depth=8 cells=354 area=49.01796 (3.8s)
[suite] ok   sync_fifo_d16_w32      gates=1261 dffs=525 depth=14 cells=2400 area=322.45128 (11.0s)
[suite] 49/49 blocks passed in 26.899s (-j8), 9/9 criteria -> out/suite/EVIDENCE.md
```

`-j N` runs N blocks at a time, each in its own process (`-j0` = one per core): 27 s on 8 cores against 78 s sequential, same results in the same order. Besides the per-block directories the suite writes `suite_table.md`, `suite_summary.json` and the two rollups `EVIDENCE.md` / `suite_evidence.json` — what the run proves about the corpus as a whole, and the sha256 of all 196 layer files. Pass an earlier run's `suite_evidence.json` to `--baseline` and the determinism criterion turns from `n/a` into a byte-for-byte comparison against it:

```sh
python3 tools/synth_area/run_suite.py -o out/suite2 -j0 --baseline out/suite/suite_evidence.json
```

Rerunning into the same `-o` directory only redoes blocks whose inputs changed: each successful block leaves a `suite_cache.json` with the sha256 of its sources, every file they `` `include `` (transitively, found next to the including file or on `-I`; one that cannot be found keeps the block uncached), include dirs (symlinked subdirectories included), profile, Liberty files, the `share/` directory yosys loads `techmap.v`/`simcells.v` from, the python/yosys/abc/sv2v/iverilog/vvp binaries a run would use *today* (resolved as `bool_area.py` does: `--yosys`/`--sv2v`, then `$YOSYS`/`$SV2V`/`$ABC`, `./build/yosys`, `PATH`; abc is `$ABC` if set, else the `yosys-abc` next to yosys — so pointing the environment at another binary reruns), the tool's own `.py` files, the full `bool_area.py` argument list and every file it wrote (fixed artifacts, `sv2v_out.v`, `equiv_*` scripts/logs, `sim/`); anything differing (or a missing/edited/appeared output) reruns. An unchanged 49-block suite rechecks in ~0.3 s and reports `49 cached`; `EVIDENCE.md` states how many blocks were reused rather than re-derived. `--no-cache` forces a full rerun, and `--baseline` always re-derives (a cached copy would make the determinism check vacuous).

**Scaling.** The same `sync_fifo` at three sizes (`examples/stress/`, not in the corpus), whole flow wall time:

| block | flops | gates | ASAP7 cells | area µm² | synth+map | graph-vs-mapped proof | total |
|---|---|---|---|---|---|---|---|
| `sync_fifo` 16×32 | 525 | 1275 | 2383 | 323.08 | 3 s | 7 s | 11 s |
| `sync_fifo_d64_w32` | 2067 | 5903 | 8795 | 1228.39 | 6 s | 63 s | 71 s |
| `sync_fifo_d1024_w8` | 8223 | 25873 | 35798 | 4949.71 | 14 s | 1085 s | 19 min |

All three `equiv=proven`, 0 simulation mismatches. The SAT proof is what scales badly, not the reporting: for area-only sweeps `--no-equiv --sim-cycles 0` keeps even the 1024-deep block at ~15 s.

With the Yosys built from this repo your numbers should match the committed ones: on one Yosys build, same RTL + same profile ⇒ byte-identical `boolean_graph.json` and netlists (`tests/test_bool_area.py::test_determinism` checks two runs against each other). A different Yosys/ABC version can legitimately produce a different, equally correct netlist; `metrics.json → tools.yosys_version` and `profile.sha256` record what each run used, so compare like with like.

## 3. What one run produces

| File | What it is |
|---|---|
| `summary.md` | One page: status, profile/tool versions, per-layer totals, cell-type table, proof and simulation verdicts, warnings, artifact list. |
| `run_manifest.json` | Provenance for the run: argv, Python/Yosys/sv2v versions, sha256 of every source, the profile and each Liberty file, and path + sha256 (or `exists: false`) of every artifact. |
| `metrics.json` | Everything machine-readable in one place: `summary` (flat), per-layer sections `word_level` / `sequential` / `boolean` / `mapped`, equivalence + simulation results, per-stage seconds, provenance. Written on failure too, with `status`, `stage`, `errors`. |
| `word_level.json` | Layer 1: the design after `proc` (before any gate lowering) as multi-bit operations — kind (`ADD`, `MUX`, `EQ`, `SHL`, `MEMRD`, `REG`, …), width, signedness, operands with widths, memories with size/ports, ports, and the `file:line` of each op. `word.ys` / `word.log` / `word_yosys.json` are the script, log and raw Yosys dump it was read from. |
| `sequential_overlay.json` | Layer 2: every register of the same checkpoint — clock + edge, reset (`sync`/`async`/`async_set`/`async_clear`, polarity, value), enable, init, `d`/`q` nets, and per bit the RTL name so it can be joined to `boolean_graph.json` node labels. |
| `boolean_graph.json` | Layer 3: one node per gate/DFF/port bit/constant, one edge per (driver → sink, pin). Any cell outside the allowed set aborts the run (`UnsupportedCell`) instead of being approximated. |
| `mapped_cells.json` | Layer 4: every ASAP7 cell instance — type, Liberty area, each pin's direction and the net on it, the output pin's Boolean function — plus per-type counts and area totals. |
| `generic_yosys.json`, `synth.ys`, `yosys.log` | The generic gate netlist Yosys produced and the exact script that produced it. |
| `mapped_yosys.json`, `mapped_netlist.v`, `stat.txt` | The ASAP7 RVT/TT netlist and Yosys' cell/area table. Five pinned NLDM Liberty files are vendored under `lib/asap7/` with sha256s that are checked on every run (`PROVENANCE.md`, BSD-3 `LICENSE`). |
| `equiv_rtl_vs_graph.{ys,log}`, `equiv_graph_vs_mapped.{ys,log}` | The two proofs. `equiv_cells` = number of compared points (outputs + same-named registers), `unproven` must be 0. |
| `sim/` | Generated testbench (`tb.v`), RTL and netlist as Icarus sees them, `sim.log`: `cycles compared_bits mismatches gate_x_bits`. |

`profiles/asap7_rvt_tt_v1.json` fixes everything that affects the numbers (frontend flags, word-level checkpoint, gate set, DFF legalisation, pass order, Liberty set). Two runs are comparable iff their `metrics.profile.sha256` match. Each layer file carries its own `schema_version`, listed in `run_manifest.json → layers`.

Reading the layers on `counter8_en`: `word_level.json` says the RTL is one `ADD` (8-bit, `count + 1`), one `NE` (the enable compare), two `MUX` (reset and enable) and one `REG`; `sequential_overlay.json` says the register is 8 bits, `clk` posedge, synchronous active-high `rst` to 0, with an enable; `boolean_graph.json` says that became 30 gates, depth 9; `mapped_cells.json` says those became 29 ASAP7 cells (8 `DFFHQx4`, 5 `AOI21`, …), 4.53 µm². The proof says all four describe the same function.

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
| `word_level.py` | Layer 1: RTLIL after `proc` → `word_level.json` (multi-bit ops, widths, signedness, memories, source lines). |
| `sequential_overlay.py` | Layer 2: `sequential_overlay.json` (per-register clock/reset/enable/init, per-bit RTL names). |
| `boolean_graph.py` | Layer 3: `write_json` → Boolean gate graph + depth/fanout metrics. |
| `mapped_cells.py` | Layer 4: Liberty parser + `mapped_cells.json` (per-cell area, function, pin-to-net). |
| `diff_sim.py` | Random differential simulation RTL vs netlist. |
| `bool_area.py` | The flow: word-level checkpoint → lower → graph → map → prove → simulate → `metrics.json`, `summary.md`, `run_manifest.json`. |
| `corpus/`, `run_suite.py`, `BOOLEAN_LAYER.md` | 49 blocks (32 with hand-counted expected metrics, 24 FIFO variants), the runner (`-j`, `--only`, `--baseline`), and the design/results write-up. |
| `suite_evidence.py` | Suite run → `EVIDENCE.md` / `suite_evidence.json`: each goal of the tool scored against the artifacts. Also runnable on an existing `suite_summary.json`. |
| `slurm_synth_area.sh`, `slurm_sweep.sh` | `sbatch` wrappers for running many blocks in parallel (fall back to local execution without `sbatch`). |
| `tests/` | `python3 tools/synth_area/tests/test_<name>.py` — all need `build/yosys`; ~2 min total. |

## 6. Not done / not claimed

- **No correlation with a commercial flow.** Until the same blocks are run through one with a matching library, the ASAP7 numbers are directional (does the edit make it bigger or smaller, by roughly how much), not calibrated.
- **Area and structure only.** No timing, power, placement; no graph rewriting or optimisation yet — this is the measurement half of the loop.
- **Word-level names are Yosys' names.** After `proc`, intermediate nets are `$procmux$4_Y`-style; ports and declared registers keep their RTL names, and every op carries its `file:line`.
- **Memories become flops.** Yosys lowers the FIFO storage to DFFs (hence 512 `DFFHQx4` in `sync_fifo`); a real flow would use a macro.
- **Multi-clock blocks** (`async_fifo`) are proven under Yosys' single-clock `async2sync` abstraction: logic preserved, CDC timing not modelled.
- **Simulation is a sample**, 200 cycles with one seed; the SAT proof is the correctness gate.
- **Slurm wrappers** have not been run on a real cluster.
- Timings above are one machine (8 cores); per-block times are single-core, the suite total is `-j8`.
- **The proof does not scale to real blocks yet.** 8 k flops already costs 18 min of SAT; a block-level design needs hierarchical or partitioned equivalence, which is not implemented.
