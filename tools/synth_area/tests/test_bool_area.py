#!/usr/bin/env python3
"""
End-to-end tests for the Boolean-layer area flow (bool_area.py). Needs a built yosys;
simulation tests are skipped without iverilog/sv2v.

    python3 tools/synth_area/tests/test_bool_area.py
"""

from __future__ import annotations

import filecmp
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
RUNNER = TOOL / "bool_area.py"
CORPUS = TOOL / "corpus"
EXAMPLES = TOOL / "examples"

sys.path.insert(0, str(TOOL))
import bool_area
import diff_sim
import synth_area

YOSYS = synth_area.find_yosys(None)
SV2V = synth_area.find_sv2v(None)
IVERILOG = shutil.which("iverilog") and shutil.which("vvp")
STRUCTURAL = ("generic_yosys.json", "mapped_yosys.json", "mapped_netlist.v", "boolean_graph.json")


def run_flow(out: Path, top: str, sources: list[Path], *extra: str) -> tuple[int, dict, str]:
    proc = subprocess.run(
        [sys.executable, str(RUNNER), *map(str, sources), "--top", top, "-o", str(out), "-q", *extra],
        capture_output=True, text=True, check=False,
    )
    metrics = json.loads((out / "metrics.json").read_text()) if (out / "metrics.json").exists() else {}
    return proc.returncode, metrics, proc.stderr


class ResetArgTests(unittest.TestCase):
    def test_parse_reset_args(self) -> None:
        self.assertIsNone(bool_area.parse_reset_args(None))
        self.assertEqual(bool_area.parse_reset_args(["rst_ni:low", "soft"]), {"rst_ni": True, "soft": False})
        for bad in ("rst_ni:lo", "rst_ni:high", ":low", "", "a:low:x"):
            with self.assertRaises(ValueError, msg=bad):
                bool_area.parse_reset_args([bad])


@unittest.skipUnless(YOSYS, "yosys binary not found")
class BoolAreaFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bool_area_test_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def flow(self, top: str, src: Path, *extra: str) -> tuple[int, dict]:
        code, m, err = run_flow(self.tmp / top, top, [src], *extra)
        self.assertTrue(m, f"no metrics.json written: {err}")
        return code, m

    def test_sync_fifo_artifacts_and_metrics(self) -> None:
        code, m = self.flow("sync_fifo", EXAMPLES / "sync_fifo.sv", "--sim-cycles", "0")
        self.assertEqual(code, 0, m["errors"])
        out = self.tmp / "sync_fifo"
        for name in (*STRUCTURAL, "metrics.json", "yosys.log", "synth.ys"):
            self.assertTrue((out / name).exists(), name)
        s, b, mp = m["summary"], m["boolean"], m["mapped"]
        self.assertEqual(s["dff"], 16 * 32 + 4 + 4 + 5)
        self.assertEqual(s["gate_total"], sum(s[g] for g in ("not", "and", "nand", "or", "nor", "xor", "xnor", "mux")))
        self.assertEqual(s["mapped_cell_total"], sum(mp["cells_by_type"].values()))
        self.assertGreater(s["mapped_cell_area"], 0)
        self.assertTrue(all(t.endswith("_ASAP7_75t_R") for t in mp["cells_by_type"]), mp["cells_by_type"])
        # graph file agrees with the metrics and every edge points at real nodes
        g = json.loads((out / "boolean_graph.json").read_text())
        ids = {n["id"] for n in g["nodes"]}
        self.assertEqual(len(g["edges"]), b["edge_total"])
        self.assertTrue(all(e["from"] in ids and e["to"] in ids for e in g["edges"]))
        self.assertEqual(sum(n["kind"] == "DFF" for n in g["nodes"]), s["dff"])
        # provenance
        self.assertEqual(m["profile"]["name"], "asap7_rvt_tt_v1")
        self.assertEqual(len(m["profile"]["liberty"]), 5)
        self.assertTrue(m["tools"]["yosys_version"])
        self.assertEqual(m["equivalence"]["status"], "proven")
        for chk in m["equivalence"]["checks"].values():
            self.assertGreaterEqual(chk["equiv_cells"], b["output_bits"])

    def test_determinism(self) -> None:
        for d in ("a", "b"):
            code, m, err = run_flow(self.tmp / d, "counter8_en", [CORPUS / "counter8_en.sv"], "--no-equiv",
                                    "--sim-cycles", "0")
            self.assertEqual(code, 0, m.get("errors", err))
        for name in STRUCTURAL:
            self.assertTrue(filecmp.cmp(self.tmp / "a" / name, self.tmp / "b" / name, shallow=False), name)
        ma = json.loads((self.tmp / "a" / "metrics.json").read_text())
        mb = json.loads((self.tmp / "b" / "metrics.json").read_text())
        self.assertEqual(ma["summary"], mb["summary"])
        self.assertEqual(ma["profile"], mb["profile"])

    def test_equivalence_catches_a_broken_netlist(self) -> None:
        # run once, then corrupt the generic netlist (swap AND->OR) and re-check with the same scripts
        code, m = self.flow("and2", CORPUS / "and2.sv", "--sim-cycles", "0")
        self.assertEqual(code, 0, m["errors"])
        out = self.tmp / "and2"
        gen = out / "generic_yosys.json"
        gen.write_text(gen.read_text().replace("$_AND_", "$_OR_"))
        script = out / "equiv_rtl_vs_graph.ys"
        proc = subprocess.run([YOSYS, "-q", "-s", str(script)], capture_output=True, text=True, check=False,
                              cwd=out)
        self.assertNotEqual(proc.returncode, 0, "equiv_status -assert must fail on a broken netlist")
        # the BMC fallback must also refuse it
        bmc = out / "equiv_rtl_vs_graph_bmc.ys"
        bmc.write_text(bool_area.equiv_bmc_script(bool_area.rtl_vs_graph_setup(
            synth_area.read_cmd_for("slang", [str(CORPUS / "and2.sv")], [], [], "and2", synth_area.SLANG_SYNTH_DEFAULTS),
            "and2", gen), 4, {}))
        proc = subprocess.run([YOSYS, "-q", "-s", str(bmc)], capture_output=True, text=True, check=False, cwd=out)
        self.assertNotEqual(proc.returncode, 0)

    def test_bounded_fallback_when_induction_fails(self) -> None:
        # shift-register FIFO: gold/gate differ on states unreachable from reset (count > depth)
        code, m = self.flow("fifo_shift_d4_w8", CORPUS / "fifo_shift_d4_w8.sv", "--sim-cycles", "0")
        self.assertEqual(code, 0, m["errors"])
        eq = m["equivalence"]
        self.assertEqual(eq["status"], "bounded")
        self.assertEqual(eq["checks"]["rtl_vs_graph"]["bmc"]["status"], "proven")
        self.assertEqual(eq["checks"]["rtl_vs_graph"]["bmc"]["resets"], ["rst_ni"])
        self.assertTrue(any("bounded proof" in w for w in m["warnings"]))
        # ... and with the fallback disabled the flow fails loudly
        code, m, _ = run_flow(self.tmp / "nobmc", "fifo_shift_d4_w8", [CORPUS / "fifo_shift_d4_w8.sv"], "--sim-cycles",
                              "0", "--equiv-bmc", "0")
        self.assertEqual(code, 1)
        self.assertEqual(m["stage"], "equivalence")

    def test_bounded_fallback_does_not_assume_zero_power_up_state(self) -> None:
        # gold's output depends on a register that no reset initialises; gate agrees with it only
        # when that register happens to be 0. A zero-initialised bounded check would accept this.
        src = self.tmp / "unreset.v"
        src.write_text(
            "module gold(input clk, input rst_n, input d, output o);\n"
            "  reg q, u;\n"
            "  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 1'b0; else q <= d;\n"
            "  always @(posedge clk) u <= u;\n"
            "  assign o = q ^ u;\n"
            "endmodule\n"
            "module gate(input clk, input rst_n, input d, output o);\n"
            "  reg q;\n"
            "  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 1'b0; else q <= d;\n"
            "  assign o = q;\n"
            "endmodule\n"
        )
        setup = [f"read_verilog {src}", "proc", "async2sync", "opt_clean"]
        script = self.tmp / "unreset_bmc.ys"
        script.write_text(bool_area.equiv_bmc_script(setup, 6, {"rst_n": True}))
        proc = subprocess.run([YOSYS, "-q", "-s", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)  # gold is x wherever u matters: unspecified, not compared
        # the other way round the gate must not be x where the RTL is defined
        src.write_text(src.read_text().replace("module gold", "module tmp").replace("module gate", "module gold")
                       .replace("module tmp", "module gate"))
        proc = subprocess.run([YOSYS, "-q", "-s", str(script)], capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        # a reachable bug is still found from the undefined start
        src.write_text(src.read_text().replace("q <= d;\n  assign o = q;", "q <= ~d;\n  assign o = q;"))
        proc = subprocess.run([YOSYS, "-q", "-s", str(script)], capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)

    def test_bounded_fallback_depth_must_cover_a_post_reset_cycle(self) -> None:
        # cycle 1 is the reset cycle and is not compared, so depth 1 would prove nothing: refused up front
        with self.assertRaises(ValueError):
            bool_area.equiv_bmc_script([], 1, {"rst_n": True})
        code, m, err = run_flow(self.tmp / "bmc1", "fifo_shift_d4_w8", [CORPUS / "fifo_shift_d4_w8.sv"], "--sim-cycles",
                                "0", "--equiv-bmc", "1")
        self.assertEqual(code, 2, err)
        self.assertIn("--equiv-bmc must be 0", err)
        self.assertEqual(m, {})
        code, _, err = run_flow(self.tmp / "bmcneg", "inv", [CORPUS / "inv.sv"], "--sim-cycles", "0", "--equiv-bmc", "-3")
        self.assertEqual(code, 2, err)
        # the smallest accepted depth still compares one post-reset cycle
        code, m, _ = run_flow(self.tmp / "bmc2", "fifo_shift_d4_w8", [CORPUS / "fifo_shift_d4_w8.sv"], "--sim-cycles",
                              "0", "--equiv-bmc", "2")
        self.assertEqual(code, 0, m.get("errors"))
        self.assertEqual(m["equivalence"]["checks"]["rtl_vs_graph"]["bmc"]["depth"], 2)

    def test_bounded_fallback_needs_a_reset(self) -> None:
        # same FIFO with a reset name inference cannot recognise: induction fails and nothing anchors a bounded proof
        src = self.tmp / "fifo_shift_d4_w8.sv"
        src.write_text((CORPUS / "fifo_shift_d4_w8.sv").read_text().replace("rst_ni", "init_ni"))
        code, m, _ = run_flow(self.tmp / "noreset", "fifo_shift_d4_w8", [src], "--sim-cycles", "0")
        self.assertEqual(code, 1)
        self.assertEqual(m["stage"], "equivalence")
        self.assertNotIn("bmc", m["equivalence"]["checks"]["rtl_vs_graph"])
        self.assertIn("no reset port", m["equivalence"]["checks"]["rtl_vs_graph"]["error"])

    def test_failures_are_reported_not_hidden(self) -> None:
        code, m = self.flow("nope", CORPUS / "inv.sv", "--sim-cycles", "0")
        self.assertNotEqual(code, 0)
        self.assertEqual(m["status"], "failed")
        self.assertIn(m["stage"], ("parse", "lowering"))
        self.assertTrue(m["errors"])
        code, m, _ = run_flow(self.tmp / "bad", "inv", [CORPUS / "inv.sv"], "--profile", "does_not_exist")
        self.assertEqual(code, 2)
        self.assertEqual(m["stage"], "profile")
        # a failed rerun into a directory with a previous good run must not leave stale artifacts behind
        out = self.tmp / "rerun"
        code, m, _ = run_flow(out, "inv", [CORPUS / "inv.sv"], "--sim-cycles", "0")
        self.assertEqual(code, 0, m["errors"])
        self.assertTrue((out / "equiv_rtl_vs_graph.log").exists())
        (self.tmp / "old_sim").mkdir()
        (self.tmp / "old_sim" / "sim.log").write_text("old\n")
        (out / "sim").symlink_to(self.tmp / "old_sim")
        code, m, _ = run_flow(out, "nope", [CORPUS / "inv.sv"], "--no-equiv", "--sim-cycles", "0")
        self.assertNotEqual(code, 0)
        self.assertFalse(any((out / name).exists() for name in STRUCTURAL))
        self.assertEqual(sorted(out.glob("equiv_*")), [])
        self.assertFalse((out / "sim").exists() or (out / "sim").is_symlink())
        self.assertTrue((self.tmp / "old_sim" / "sim.log").exists(), "symlink target must not be deleted")

    def test_malformed_profile_is_a_structured_failure(self) -> None:
        good = json.loads((TOOL / "profiles" / "asap7_rvt_tt_v1.json").read_text())
        bad = {
            "not_json": "{ this is not json",
            "not_object": "[1, 2]",
            "missing_key": json.dumps({k: v for k, v in good.items() if k != "liberty"}),
            "wrong_type": json.dumps({**good, "dff_types": "DFF_P"}),
            "bad_script": json.dumps({**good, "script": {"lower": "not a list", "map": []}}),
            "bad_script_entry": json.dumps({**good, "script": {"lower": [1], "map": []}}),
            "bad_placeholder": json.dumps({**good, "script": {"lower": ["synth -top {nope}"], "map": []}}),
            "nested_placeholder": json.dumps({**good, "script": {"lower": ["synth -top {top.foo}"], "map": []}}),
            "indexed_placeholder": json.dumps({**good, "script": {"lower": ["synth -top {top[0]}"], "map": []}}),
            "unbalanced_brace": json.dumps({**good, "script": {"lower": ["synth -top {top"], "map": []}}),
            "format_spec": json.dumps({**good, "script": {"lower": ["synth -top {top:foo}"], "map": []}}),
            "conversion": json.dumps({**good, "script": {"lower": ["synth -top {top!r}"], "map": []}}),
            "bad_frontend": json.dumps({**good, "frontend": {"name": "vivado"}}),
            "bad_dff_entry": json.dumps({**good, "dff_types": [["$_DFF_P_"]]}),
            "bad_lib_entry": json.dumps({**good, "liberty": {**good["liberty"], "files": [{"file": 1}]}}),
            "no_lib_files": json.dumps({**good, "liberty": {**good["liberty"], "files": []}}),
        }
        for name, text in bad.items():
            prof = self.tmp / f"{name}.json"
            prof.write_text(text)
            code, m, err = run_flow(self.tmp / name, "inv", [CORPUS / "inv.sv"], "--profile", str(prof), "--sim-cycles", "0")
            self.assertEqual(code, 2, (name, err))
            self.assertEqual(m["status"], "failed", name)
            self.assertEqual(m["stage"], "profile", name)
            self.assertNotIn("Traceback", err, name)
            self.assertIn(str(prof), m["errors"][0], name)
        # a liberty entry that exists but is a directory is a profile error, not an IsADirectoryError later
        prof = self.tmp / "lib_is_dir.json"
        prof.write_text(json.dumps({**good, "liberty": {"dir": ".", "files": [{"file": "corpus", "sha256": "0" * 64}]}}))
        for verify in ([], ["--no-verify-libs"]):
            code, m, err = run_flow(self.tmp / f"lib_is_dir{len(verify)}", "inv", [CORPUS / "inv.sv"], "--profile",
                                    str(prof), "--sim-cycles", "0", *verify)
            self.assertEqual((code, m["stage"]), (2, "profile"), err)
            self.assertNotIn("Traceback", err)
            self.assertIn("not a regular file", m["errors"][0])

    def test_unusable_tools_are_structured_failures(self) -> None:
        not_exec = self.tmp / "yosys_not_executable"
        not_exec.write_text("#!/bin/sh\nexit 0\n")
        not_exec.chmod(0o644)
        code, m, err = run_flow(self.tmp / "noexec", "inv", [CORPUS / "inv.sv"], "--yosys", str(not_exec), "--sim-cycles", "0")
        self.assertEqual((code, m["stage"]), (2, "tools"), err)
        self.assertNotIn("Traceback", err)
        self.assertIn("not executable", m["errors"][0])
        code, m, err = run_flow(self.tmp / "nosv2v", "inv", [CORPUS / "inv.sv"], "--frontend", "sv2v", "--sv2v",
                                str(not_exec), "--sim-cycles", "0")
        self.assertEqual((code, m["stage"]), (2, "tools"), err)
        self.assertNotIn("Traceback", err)
        self.assertIn("sv2v not executable", m["errors"][0])

    def test_source_identity_in_metrics(self) -> None:
        code, m = self.flow("inv", CORPUS / "inv.sv", "--no-equiv", "--sim-cycles", "0")
        self.assertEqual(code, 0, m["errors"])
        (src,) = m["sources"]
        self.assertEqual(src["path"], str((CORPUS / "inv.sv").resolve()))
        self.assertEqual(src["sha256"], bool_area.sha256_file(CORPUS / "inv.sv"))
        # a missing source is an input error that still records the (hash-less) path
        code, m, _ = run_flow(self.tmp / "missing", "inv", [self.tmp / "missing.sv"], "--sim-cycles", "0")
        self.assertEqual(code, 2)
        self.assertEqual(m["stage"], "inputs")
        self.assertIsNone(m["sources"][0]["sha256"])

    def test_explicit_sim_ports_are_validated(self) -> None:
        src = CORPUS / "counter8_en.sv"
        for extra in (("--sim-reset", "typo:low"), ("--sim-clock", "nope"), ("--sim-reset", "q")):
            code, m, err = run_flow(self.tmp / extra[1].replace(":", "_"), "counter8_en", [src], *extra, "--sim-cycles", "0")
            self.assertEqual(code, 2, (extra, err))
            self.assertEqual(m["stage"], "inputs", extra)
            self.assertIn(extra[1].split(":")[0], m["errors"][0], extra)
            self.assertIn("not an input port", m["errors"][0], extra)
            self.assertIsNone(m["equivalence"], "must fail before any proof runs")
        # a malformed polarity suffix is rejected rather than silently read as active-high
        code, m, err = run_flow(self.tmp / "badpol", "counter8_en", [src], "--sim-reset", "rst_n:lo", "--sim-cycles", "0")
        self.assertEqual(code, 2, err)
        self.assertEqual(m["stage"], "inputs")
        self.assertIn("PORT:low", m["errors"][0])
        self.assertNotIn("Traceback", err)
        # the correctly named ports are accepted (and the bounded fallback still sees the reset)
        code, m = self.flow("fifo_shift_d4_w8", CORPUS / "fifo_shift_d4_w8.sv", "--sim-clock", "clk_i", "--sim-reset",
                            "rst_ni:low", "--sim-cycles", "0")
        self.assertEqual(code, 0, m["errors"])
        self.assertEqual(m["equivalence"]["checks"]["rtl_vs_graph"]["bmc"]["resets"], ["rst_ni"])

    @unittest.skipUnless(SV2V and IVERILOG, "sv2v + iverilog needed for differential simulation")
    def test_differential_simulation(self) -> None:
        code, m = self.flow("async_fifo", EXAMPLES / "async_fifo.sv", "--no-equiv", "--sim-cycles", "50")
        self.assertEqual(code, 0, m["errors"])
        sim = m["simulation"]
        self.assertEqual(sim["status"], "match", sim)
        self.assertEqual(sim["mismatches"], 0)
        self.assertGreater(sim["compared_bits"], 0)
        self.assertEqual(sim["clocks"], ["wclk_i", "rclk_i"])
        self.assertEqual(sim["resets"], {"wrst_ni": "active_low", "rrst_ni": "active_low"})

    @unittest.skipUnless(SV2V and IVERILOG, "sv2v + iverilog needed for differential simulation")
    def test_differential_simulation_catches_a_broken_netlist(self) -> None:
        code, m = self.flow("xor2", CORPUS / "xor2.sv", "--no-equiv", "--sim-cycles", "0")
        self.assertEqual(code, 0, m["errors"])
        out = self.tmp / "xor2"
        mapped = out / "mapped_yosys.json"
        graph = json.loads((out / "boolean_graph.json").read_text())
        libs = bool_area.liberty_paths(bool_area.load_profile("asap7_rvt_tt_v1")[0], verify=False)
        # swap the XOR cell for an XNOR of the same footprint
        mapped.write_text(mapped.read_text().replace("XOR2xp5_ASAP7_75t_R", "XNOR2xp5_ASAP7_75t_R"))
        res = diff_sim.run_diff_sim(
            top="xor2", ports=graph["ports"], rtl_sources=[str(CORPUS / "xor2.sv")], includes=[], defines=[],
            mapped_json=mapped, libs=libs, yosys=YOSYS, sv2v=SV2V, iverilog=shutil.which("iverilog"),
            vvp=shutil.which("vvp"), work=out / "sim2", cycles=20, seed=3, timeout=120,
        )
        self.assertEqual(res["status"], "mismatch", res)
        self.assertGreater(res["mismatches"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
