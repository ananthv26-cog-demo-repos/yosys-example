# synth_area — SystemVerilog block → Yosys → JSON area report

A small CLI that takes one RTL block, synthesizes it with Yosys, and writes a
machine-readable report of how big it came out (cell counts, flop count, and
area when a cell library is supplied). It is the fast, cheap stand-in for a
commercial synthesis run: seconds instead of hours, no license limit, so many
candidate RTL changes can be measured in parallel and only the promising ones
sent to the slow, accurate signoff flow.

```
SystemVerilog ──► read_slang (or sv2v) ──► synth ──► [dfflibmap/abc -liberty] ──► stat -json ──► report.json
```

## Requirements

* A Yosys built from this repo (`cmake -B build -G Ninja . && cmake --build build`).
  The slang SystemVerilog frontend (`read_slang`) is built in by default.
* Python 3.10+.
* Optional: [`sv2v`](https://github.com/zachjs/sv2v) on `PATH` (second SystemVerilog path).
* Optional: a Liberty `.lib` cell library for real area numbers. Without one,
  the report contains generic gate counts and a CMOS transistor estimate.

## Usage

```sh
# generic gate counts (no cell library)
tools/synth_area/synth_area.py --top sync_fifo tools/synth_area/examples/sync_fifo.sv -o out/sync_fifo.json

# real area with a Liberty library, keep the mapped netlist
tools/synth_area/synth_area.py --top struct_fifo \
    tools/synth_area/examples/fifo_pkg.sv tools/synth_area/examples/struct_fifo.sv \
    --liberty /path/to/cells.lib --netlist -o out/struct_fifo.json

# includes / defines / a specific frontend
tools/synth_area/synth_area.py --top cc_fifo -I include src/cc_pkg.sv src/cc_fifo.sv --frontend slang -D SYNTHESIS -o out/cc_fifo.json
```

One line is printed on success:

```
[synth_area] OK top=sync_fifo frontend=slang cells=1750 flops=525 area=4243.232 (0.46s) -> out/sync_fifo.json
```

Exit code: `0` success, `1` synthesis/elaboration failed, `2` bad invocation
(missing file, no yosys). The JSON report is written in every case.

### Options worth knowing

| flag | meaning |
|---|---|
| `--frontend auto\|slang\|sv2v\|verilog` | `auto` (default) tries slang, then sv2v, then plain `read_verilog -sv`. Pin one frontend when comparing runs (see below). |
| `--liberty FILE` | map to the library's cells and report `area` (Liberty units, usually µm²). |
| `--empty-blackboxes` | treat modules with empty bodies as black boxes — supply stub modules for SRAM macros / tech cells and their instances show up as cells in the report. |
| `--keep-assertions` | by default slang is run with `--ignore-assertions --ignore-initial --ignore-timing`; this turns that off. |
| `--slang-arg=--foo` | pass any other `read_slang` option (e.g. `--allow-use-before-declare`, `--compat vcs`). |
| `--no-flatten` | keep hierarchy (numbers are then per design, hierarchy preserved in the netlist). |
| `--netlist` | also write the mapped netlist as Verilog (for OpenSTA, equivalence checks, inspection). |
| `--yosys / --sv2v` | tool paths; also `$YOSYS` / `$SV2V`. Default yosys is `./build/yosys` in this repo, then `PATH`. |

## Report format

```jsonc
{
  "schema_version": 1,
  "status": "ok",                       // or "failed"
  "top": "sync_fifo",
  "frontend_requested": "auto",
  "frontend_used": "slang",
  "frontends_tried": ["slang"],
  "sources": ["/abs/path/sync_fifo.sv"],
  "include_dirs": [], "defines": [],
  "liberty": "/abs/path/cells.lib",     // null when not given
  "flatten": true,
  "yosys_version": "Yosys 0.69+ (git sha1 ...)",
  "sv2v_version": "sv2v v0.0.13",
  "work_dir": ".../sync_fifo.work",     // logs, yosys script, netlist
  "log": ".../yosys_slang.log",
  "netlist": null,
  "stats": {
    "num_cells": 1750,
    "num_flops": 525,                   // cells whose type looks like a flop/latch
    "num_comb_cells": 1225,
    "num_wires": 1743, "num_wire_bits": 2321,
    "num_ports": 9, "num_port_bits": 75,
    "num_memories": 0, "num_memory_bits": 0,
    "cells_by_type": {"MUX2_X1": 656, "DFF_X1": 512, "...": 0},
    "area": 4243.232,                   // with --liberty
    "area_unit": "liberty area units",
    "sequential_area": 2384.424,
    "area_is_lower_bound": false,       // true if some cell types are not in the .lib (black boxes, unmapped $ cells)
    "unknown_area_cell_types": {}       // e.g. {"sram_macro": 1}
    // without --liberty instead:
    // "estimated_transistors": 6222, "estimated_transistors_is_lower_bound": true
  },
  "stages": [ {"name": "yosys[slang]", "ok": true, "seconds": 0.41, "command": "...", ...} ],
  "warnings": ["..."],
  "errors": [],                         // empty on success (failed earlier frontends are demoted to warnings);
                                        // on failure the first entry is the actionable diagnostic
  "wall_seconds": 0.458
}
```

## Comparing runs

```sh
tools/synth_area/compare_reports.py baseline.json candidate.json [...]
```

prints cells / flops / area (or transistor estimate) and the % delta against
the first report; `--json` for machines. A candidate gets no delta (and a
`not_comparable` reason) when it failed, used a different frontend or library
than the baseline, or either report's Liberty area is only a lower bound because
some cell types (black boxes, unmapped cells, cells declared without `area`, or
a library with no `area` at all) are not counted (`--allow-partial` overrides the
last one). A library whose cells legitimately carry `area : 0` is complete, not
partial — the runner asks yosys' own Liberty reader whether any cell has an
`area` attribute rather than inferring it from a zero total. The frontend rule
matters: the frontends elaborate some constructs
differently and the resulting gate counts can differ substantially for the same
RTL (e.g. `cc_fifo` from pulp-platform/common_cells: 1079 cells via slang vs
1779 via sv2v). Slang is the default because it passes the most constructs and
gives the smaller netlists in our samples.

## Slurm

`slurm_synth_area.sh` submits one run with `sbatch --wrap` (arguments are passed
through unchanged) and prints the job id; `slurm_sweep.sh blocks.txt outdir`
submits one job per line of a block list (`<top> <sources...>`). Resources are
set through `SYNTH_AREA_PARTITION`, `SYNTH_AREA_CPUS` (default 1 — Yosys is
single-threaded), `SYNTH_AREA_MEM`, `SYNTH_AREA_TIME`, `SYNTH_AREA_SBATCH_ARGS`.
Without `sbatch` on `PATH` (or with `SYNTH_AREA_LOCAL=1`) both scripts run the
jobs in the current shell, so the same command line works on a laptop and on
the cluster. The compute node needs the built `yosys` reachable via `$YOSYS`,
`PATH`, or the repo's `build/` directory (a shared filesystem is enough).

## What the numbers mean (and don't)

* **No cell library → no physical area.** `estimated_transistors` is Yosys'
  rough CMOS model (`stat -tech cmos`); the `_is_lower_bound` flag is set when
  some cells (e.g. async-reset flops) have no model. Good for "bigger/smaller",
  not for absolute size. With the real process `.lib` the `area` field is in
  the same units the commercial flow reports.
* **Memories become flops.** Yosys has no SRAM macros unless you give it some;
  a 16×32 FIFO storage array turns into 512 `DFF` cells. This is consistent
  between runs (so deltas are still meaningful) but will differ from a
  commercial flow that maps to a memory compiler. Use `--empty-blackboxes` with stub
  modules for the macros to keep them out of the count.
* **Area only, no timing yet.** The mapped netlist (`--netlist`) plus the
  Liberty file is exactly what OpenSTA needs for a flop-to-flop timing
  estimate; that is the natural next step.
* **Single-threaded.** One block takes well under a second for FIFO-sized
  designs; the speedup comes from running hundreds of jobs in parallel on
  Slurm, not from faster machines.

## Tests and the SystemVerilog support matrix

```sh
python3 tools/synth_area/tests/test_synth_area.py    # runner smoke tests (needs built yosys)
python3 tools/synth_area/sv_matrix.py --md matrix.md   # regenerate the table
```

`sv_probes/` holds one tiny module per SystemVerilog construct; `sv_matrix.py`
runs each through every frontend and writes the table that is pasted into the
"Full matrix" section of [SV_SUPPORT.md](SV_SUPPORT.md), which holds the findings.
