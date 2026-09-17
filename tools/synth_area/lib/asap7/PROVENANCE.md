# ASAP7 7.5-track standard cells — pinned Liberty files

These are the **only** technology-mapping target of the `asap7_rvt_tt_v1` synthesis profile
(`tools/synth_area/profiles/asap7_rvt_tt_v1.json`). Every experiment run through
`bool_area.py` maps to exactly these files, so cell counts and summed areas are comparable
across runs, machines and Slurm jobs.

| property | value |
|---|---|
| library | `asap7sc7p5t_28` (ASAP7 PDK, 7.5-track cells, rev 28) — ASU / ARM predictive 7nm |
| threshold flavour | RVT (`_R` cell suffix) |
| corner | TT (typical process, 0.70 V, 25 C) |
| delay model | NLDM (`.lib` timing tables; only the `area` values and pin functions are used here) |
| cell groups | AO, INVBUF, OA, SEQ, SIMPLE (the same five files OpenROAD-flow-scripts uses for `TC_NLDM_LIB_FILES`) |
| copied from | `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `fbc8fa48d2f0864d1cfd0536bada7893a3f27b9c`, `flow/platforms/asap7/lib/NLDM/` |
| upstream | https://github.com/The-OpenROAD-Project/asap7sc7p5t_28 (`LIB/NLDM/*.lib.7z`) |
| license | BSD-3-Clause (see `LICENSE` in this directory) |
| area unit | µm² (7.5-track cell height 0.270 µm; e.g. `INVx1` = 3 CPP × 54 nm × 0.270 µm = 0.04374) |

`asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib` is stored uncompressed upstream; it is gzipped here
so all five files are handled identically (Yosys reads `.gz` transparently).

SHA-256 of the **uncompressed** `.lib` content (identity of the upstream data):

```
b1ece108c0a2acd8d01c05cee6daee2834425037b1d9aad625d6f1dd640f1940  asap7sc7p5t_AO_RVT_TT_nldm_211120.lib
a4ceb32e418e1aac8667e28fbd8e9e46baf520180be4db075598a4d7c681a56e  asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib
6529c6c72465cd133dfbfb03952327ab2774bb6aa4be866d78fd7bc6c58c9f6f  asap7sc7p5t_OA_RVT_TT_nldm_211120.lib
57a0b403485b99ebd676942af4673ac086b86c7c75fbdc3e5c0038501dd22ba3  asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib
fa92e6ab1481810602811b1eea54bc016a341f11fb5188d7512c026599adf038  asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib
```

The SHA-256 of the `.gz` files as committed is recorded in the profile and verified by
`bool_area.py` before every run (`--no-verify-libs` skips it).

Not included: LVT/SLVT/SRAM flavours, FF/SS corners, CCS models, LEF/GDS, Verilog cell models.
Equivalence checking of the mapped netlist uses the pin `function` / `ff` groups from these
Liberty files (Yosys `read_liberty` without `-lib`), so no Verilog models are needed.
