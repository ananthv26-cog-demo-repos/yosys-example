# SystemVerilog support in Yosys: what works today

Findings from running the probes in `sv_probes/` and the FIFOs in `examples/`
through the three ways of getting SystemVerilog into Yosys. The table under
"Full matrix" is the verbatim output of `python3 sv_matrix.py --md matrix.md`;
to refresh it, rerun that and paste `matrix.md` over the table.

Tested with Yosys 0.69+ (this fork, commit 150c32b03, slang frontend 11.0.0)
and sv2v v0.0.13, on Ubuntu 22.04.

## Summary

* **The "Yosys can't read SystemVerilog" concern is out of date.** Since
  Yosys 0.6x the slang-based frontend (`read_slang`, a.k.a. sv-elab) ships in
  the Yosys tree and is built by default (`YOSYS_WITHOUT_SLANG=OFF`). It
  handled 33 of 34 probes including packages, interfaces with modports, packed
  structs/unions, enums, type parameters, `unique`/`priority`, `inside`,
  streaming operators, `let`, automatic functions with `break`, immediate and
  concurrent assertions (ignored for synthesis), DPI imports and class
  declarations (ignored), and all four FIFOs. No patches to Yosys were needed.
* **The only slang failure** was enum iteration methods (`.next()`, `.first()`),
  reported as `unsupported system task 'next'`. Rewrite as explicit
  arithmetic/case, or file upstream at povik/sv-elab.
* **sv2v is a good second opinion** (30/34). It fails on `let`, `class`
  declarations and DPI imports (its Verilog output is not accepted by
  `read_verilog`), i.e. on testbench-flavoured constructs that should not be in
  synthesizable RTL anyway.
* **Yosys' native `read_verilog -sv` is not enough** (16/34): no packages, no
  type parameters, no unpacked array ports, no `break`, no assertions, no
  `inside`, no struct parameters. It fails on 3 of the 4 FIFOs.
* **Frontends are not interchangeable for area numbers.** For the same RTL,
  slang and sv2v produced different netlists after identical `synth`
  (`struct_fifo` 393 vs 571 cells, `if_fifo_top` 150 vs 205,
  pulp-platform `cc_fifo` 1079 vs 1779). Comparisons across runs must use the
  same frontend; the runner defaults to slang and records `frontend_used` in
  every report.
* **Real-world code:** `cc_fifo`, `cc_stream_fifo`, `cc_passthrough_stream_fifo`
  from pulp-platform/common_cells (packages, `include` macros for assertions
  and registers, parameter types) synthesized unmodified with `-I include`.
  `cc_cdc_fifo_gray` failed only because it instantiates `tc_sync` from another
  repo; with a two-line stub module and `--empty-blackboxes` it synthesized and
  the 8 `tc_sync` instances appear as black-box cells in the report.

## Things to know when pointing this at production RTL

* **Unknown modules are hard errors** in sv-elab (`--ignore-unknown-modules`
  was removed upstream). Provide every module, or an empty stub plus
  `--empty-blackboxes`. SRAM macros and tech cells (clock gates, synchronizers)
  will need stubs, which is also what keeps them out of the gate count.
* **Assertions, `initial` blocks and delays are ignored by default**
  (`--ignore-assertions --ignore-initial --ignore-timing`). `-D SYNTHESIS` is
  added implicitly by the frontend, so ``ifdef SYNTHESIS` guards work.
* **`string` parameters and `$display` inside `initial`** are fine when ignored
  (p29 passes only because initial blocks are dropped; with `--keep-assertions`
  it fails with "expression of type string ... unsupported for synthesis").
* **Vendor-specific slang knobs** are reachable via `--slang-arg`, e.g.
  `--slang-arg=--compat --slang-arg=vcs`, `--allow-use-before-declare`,
  `--allow-hierarchical-const`, `--allow-toplevel-iface-ports`.
* **Memories become flops** unless black-boxed (see README).

## Full matrix

Cell counts are generic gates after `synth -flatten` (no cell library) and are
only there to show whether the frontends agree; "0 cells" means the probe is
pure wiring.

| construct | slang | sv2v | verilog |
|---|---|---|---|
| p01_always_ff_comb: always_ff / always_comb, logic type, non-blocking reset | ok (2 cells) | ok (2 cells) | ok (2 cells) |
| p02_sv_types: SV data types: logic, bit, byte, shortint, int, unsigned, 4-state literals | ok (57 cells) | ok (57 cells) | ok (57 cells) |
| p03_packed_struct: typedef packed struct, member access on ports and internally | ok (24 cells) | ok (24 cells) | ok (24 cells) |
| p04_unpacked_array_port: unpacked array port and array-of-array memory | ok (56 cells) | ok (56 cells) | FAIL: p04_unpacked_array_port.sv:2: ERROR: syntax error, unexpected '[', expecting ')' or ',' or '=' |
| p05_enum: enum typedef, enum-typed state register, enum literal comparison | ok (8 cells) | ok (8 cells) | ok (8 cells) |
| p06_package_import: package with parameters, typedef and function; import into module header | ok (22 cells) | ok (22 cells) | FAIL: p06_package_import.sv:5: ERROR: syntax error, unexpected TOK_ID |
| p07_interface_modport: interface with modports passed to a submodule | ok (4 cells) | ok (4 cells) | ok (4 cells) |
| p08_parameter_type: type parameters and parameterized type widths | ok (16 cells) | ok (16 cells) | FAIL: p08_parameter_type.sv:2: ERROR: syntax error, unexpected TOK_ID, expecting ')' or ',' or '=' |
| p09_unique_priority_case: unique case / priority if / case inside | ok (6 cells) | ok (6 cells) | ok (6 cells) |
| p10_always_latch: always_latch | ok (4 cells) | ok (4 cells) | ok (4 cells) |
| p11_function_automatic: automatic function with loop, break, local variables, return | ok (18 cells) | ok (21 cells) | FAIL: p11_function_automatic.sv:5: ERROR: Can't resolve task name `\break'. |
| p12_generate: generate for/if with named blocks and genvar in localparam | ok (6 cells) | ok (6 cells) | ok (6 cells) |
| p13_streaming_op: streaming operator (bit reverse) and replication | ok (0 cells) | ok (0 cells) | FAIL: p13_streaming_op.sv:3: ERROR: syntax error, unexpected OP_SHL |
| p14_immediate_assert: immediate assertion + $error in always_ff (should be ignored for synthesis) | ok (4 cells) | ok (4 cells) | FAIL: p14_immediate_assert.sv:5: ERROR: syntax error, unexpected TOK_ELSE, expecting ';' |
| p15_concurrent_sva: concurrent SVA property/assert with \|=> and $past | ok (1 cells) | ok (1 cells) | FAIL: p15_concurrent_sva.sv:4: ERROR: syntax error, unexpected TOK_PROPERTY |
| p16_dpi_import: DPI-C import (not synthesizable; expect frontend to reject or ignore) | ok (8 cells) | FAIL: sv2v_out.v:9: ERROR: syntax error, unexpected '[', expecting TOK_ID or ':' or '=' | FAIL: p16_dpi_import.sv:3: ERROR: syntax error, unexpected TOK_ID, expecting ')' or ',' |
| p17_inside_operator: inside operator with ranges and wildcard equality | ok (19 cells) | ok (17 cells) | FAIL: p17_inside_operator.sv:3: ERROR: syntax error, unexpected TOK_ID |
| p18_multidim_packed: multi-dimensional packed arrays and part-selects | ok (24 cells) | ok (24 cells) | ok (24 cells) |
| p19_packed_union: packed union | ok (0 cells) | ok (0 cells) | ok (0 cells) |
| p20_int_loop_var: loop variable declared in for header, compound assignment, ++ | ok (26 cells) | ok (26 cells) | ok (26 cells) |
| p21_size_casts: size/sign casts and system functions $bits/$size/$clog2 | ok (64 cells) | ok (64 cells) | ok (64 cells) |
| p22_implicit_port_conn: .name and .* implicit port connections | ok (4 cells) | ok (4 cells) | ok (4 cells) |
| p23_enum_methods: enum methods .next()/.first() and enum casts | FAIL: p23_enum_methods.sv:6:70: error: unsupported system task 'next' | FAIL: sv2v_out.v:12: ERROR: Can't resolve function name `\s.first'. | FAIL: p23_enum_methods.sv:6: ERROR: Can't resolve function name `\s.first'. |
| p24_ifdef_macros: `define with args, `ifdef/`else, `include-free macro usage | ok (4 cells) | ok (4 cells) | ok (4 cells) |
| p25_wire_logic_mixing: var/wire keywords, tri, default_nettype none | ok (1 cells) | ok (1 cells) | ok (1 cells) |
| p26_struct_array_memory: array of packed structs as memory with struct-member write | ok (65 cells) | ok (65 cells) | FAIL: p26_struct_array_memory.sv:5: ERROR: Index in generate block prefix syntax is not constant! |
| p27_parameter_struct: parameter of struct type with struct literal default | ok (0 cells) | ok (0 cells) | FAIL: p27_parameter_struct.sv:4: ERROR: syntax error, unexpected ':' |
| p28_let_and_const: const variables, let declarations | ok (7 cells) | FAIL: sv2v failed: p28_let_and_const.sv:4:5: Parse error: missing expected `endmodule` | FAIL: p28_let_and_const.sv:4: ERROR: syntax error, unexpected '=', expecting ',' or ';' |
| p29_string_param_display: string parameter + initial $display (should synthesize to nothing extra) | ok (1 cells) | ok (2 cells) | FAIL: p29_string_param_display.sv:2: ERROR: syntax error, unexpected TOK_ID, expecting ')' or ',' or '=' |
| p30_class_decl: class declaration (testbench-only construct) next to synthesizable logic | ok (1 cells) | FAIL: sv2v failed: p30_class_decl.sv:3:5: Parse error: missing expected `endmodule` | FAIL: p30_class_decl.sv:3: ERROR: syntax error, unexpected ';', expecting '(' or '[' |
| sync_fifo | ok (1077 cells) | ok (1077 cells) | ok (1077 cells) |
| struct_fifo (pkg+struct+enum) | ok (393 cells) | ok (571 cells) | FAIL: fifo_pkg.sv:17: ERROR: syntax error, unexpected '?', expecting ';' |
| if_fifo_top (interfaces) | ok (150 cells) | ok (205 cells) | FAIL: ERROR: Module `$paramod$698d5f4fdaf5ca1e41fde98124b2bf97706c1f76\if_fifo_core$interfaces$$paramod\fifo_port_if\Width=32'00000000000000000000000000010000$paramod |
| async_fifo (2 clocks, gray) | ok (373 cells) | ok (374 cells) | FAIL: async_fifo.sv:18: ERROR: syntax error, unexpected TOK_ID |
| **passing** | 33/34 | 30/34 | 16/34 |
