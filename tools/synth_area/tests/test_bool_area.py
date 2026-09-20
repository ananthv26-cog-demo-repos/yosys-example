#!/usr/bin/env python3
"""
End-to-end tests for the Boolean-layer area flow (bool_area.py). Needs a built yosys;
simulation tests are skipped without iverilog/sv2v.

    python3 tools/synth_area/tests/test_bool_area.py
"""

from __future__ import annotations

import filecmp
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        # per-class depth: all outputs are registered so there is no in2out path; the aggregate is the
        # max over the classes, and the flat summary + summary.md carry the same numbers
        paths = b["depth_by_path"]
        self.assertEqual(set(paths), {"reg2reg", "in2reg", "reg2out", "in2out"})
        self.assertIsNone(paths["in2out"])
        present = {c: p for c, p in paths.items() if p is not None}
        self.assertEqual(set(present), {"reg2reg", "in2reg", "reg2out"})
        self.assertEqual(max(p["depth"] for p in present.values()), b["max_depth"])
        self.assertTrue(all(p["from"] and p["to"] for p in present.values()), paths)
        self.assertEqual({c: s[f"depth_{c}"] for c in paths}, {c: p and p["depth"] for c, p in paths.items()})
        summary_md = (out / "summary.md").read_text()
        r2r = paths["reg2reg"]
        self.assertIn(f"| reg2reg | {r2r['depth']} | `{r2r['from']}` | `{r2r['to']}` |", summary_md)
        self.assertIn("| in2out | none | | |", summary_md)
        # provenance
        self.assertEqual(m["profile"]["name"], "asap7_rvt_tt_v1")
        self.assertEqual(len(m["profile"]["liberty"]), 5)
        self.assertTrue(m["tools"]["yosys_version"])
        self.assertEqual(m["equivalence"]["status"], "proven")
        for chk in m["equivalence"]["checks"].values():
            self.assertGreaterEqual(chk["equiv_cells"], b["output_bits"])
        # RTL vs graph needs the full induction depth; graph vs mapped (registers paired 1:1) is proven at 1
        checks = m["equivalence"]["checks"]
        self.assertEqual((checks["rtl_vs_graph"]["seq"], checks["graph_vs_mapped"]["seq"]), (5, 1))
        self.assertIn("equiv_induct -seq 1", (out / "equiv_graph_vs_mapped.ys").read_text())
        self.assertEqual(checks["graph_vs_mapped"]["log"], str(out / "equiv_graph_vs_mapped.log"))
        self.assertFalse(list(out.glob("equiv_*_seq*")), "no retry happened, so no kept short attempt")

    def flow_with_mapped_seq1_unproven(self, out: Path, *extra: str) -> tuple[int, dict, list[int]]:
        """Run and2 in-process with graph-vs-mapped `-seq 1` faked to leave every cell unproven."""
        real, seqs = bool_area.run_yosys, []

        def fake(yosys: str, script: str, script_path: Path, log_path: Path, timeout: int):
            if "read_liberty" in script and "equiv_induct" in script:
                seqs.append(int(script.split("equiv_induct -seq ")[1].split()[0]))
                if seqs[-1] == 1:
                    script_path.write_text(script)
                    log_path.write_text("Found 3 $equiv cells in 1 modules.\n"
                                        "  Of those cells 0 are proven and 3 are unproven.\n"
                                        "ERROR: Found 3 unproven $equiv cells!\n")
                    return False, log_path.read_text(), 0.25
            return real(yosys, script, script_path, log_path, timeout)

        with mock.patch.object(bool_area, "run_yosys", fake):
            code = bool_area.main([str(CORPUS / "and2.sv"), "--top", "and2", "-o", str(out), "-q", "--sim-cycles", "0",
                                   *extra])
        return code, json.loads((out / "metrics.json").read_text()), seqs

    def test_mapped_proof_retries_at_full_depth_when_short_induction_leaves_cells_unproven(self) -> None:
        out = self.tmp / "retry"
        code, m, seqs = self.flow_with_mapped_seq1_unproven(out)
        self.assertEqual(code, 0, m["errors"])
        self.assertEqual(seqs, [1, 5])
        chk = m["equivalence"]["checks"]["graph_vs_mapped"]
        self.assertEqual((chk["status"], chk["seq"], chk["unproven"]), ("proven", 5, 0))
        self.assertGreater(chk["seconds"], 0.25, "seconds covers both attempts")
        self.assertEqual(m["timing"]["equiv_graph_vs_mapped_seconds"], chk["seconds"])
        self.assertTrue(any("3 cells not proven by -seq 1" in w and "retrying at -seq 5" in w for w in m["warnings"]),
                        m["warnings"])
        self.assertEqual(m["equivalence"]["status"], "proven")
        # the short attempt is kept beside the deeper one that replaced it, and both are flow-owned outputs
        self.assertIn("3 are unproven", (out / "equiv_graph_vs_mapped_seq1.log").read_text())
        self.assertIn("equiv_induct -seq 1", (out / "equiv_graph_vs_mapped_seq1.ys").read_text())
        self.assertIn("equiv_induct -seq 5", (out / "equiv_graph_vs_mapped.ys").read_text())
        self.assertEqual(chk["log"], str(out / "equiv_graph_vs_mapped.log"))
        owned = {p.name for p in bool_area.owned_files(out)}
        self.assertTrue({"equiv_graph_vs_mapped_seq1.ys", "equiv_graph_vs_mapped_seq1.log"} <= owned, owned)
        # RTL vs graph is unaffected: one attempt at the configured depth
        self.assertEqual(m["equivalence"]["checks"]["rtl_vs_graph"]["seq"], 5)
        self.assertFalse(list(out.glob("equiv_rtl_vs_graph_seq*")))

    def test_mapped_short_induction_can_be_skipped_or_is_the_only_attempt(self) -> None:
        code, m, seqs = self.flow_with_mapped_seq1_unproven(self.tmp / "skip", "--equiv-mapped-seq", "0")
        self.assertEqual(code, 0, m["errors"])
        self.assertEqual(seqs, [5])
        self.assertEqual(m["equivalence"]["checks"]["graph_vs_mapped"]["seq"], 5)
        self.assertFalse(list((self.tmp / "skip").glob("equiv_*_seq*")))
        # equal depths run once; a short depth that fails with no deeper one to fall back to fails closed
        code, m, seqs = self.flow_with_mapped_seq1_unproven(self.tmp / "same", "--equiv-seq", "1", "--equiv-bmc", "0")
        self.assertEqual(seqs, [1])
        self.assertEqual((code, m["stage"], m["equivalence"]["checks"]["graph_vs_mapped"]["status"]),
                         (1, "equivalence", "failed"))

    def test_equiv_depths_are_validated(self) -> None:
        for bad in (("--equiv-seq", "0"), ("--equiv-mapped-seq", "-1")):
            code, _, err = run_flow(self.tmp / "bad", "and2", [CORPUS / "and2.sv"], *bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("--equiv-seq must be at least 1", err)

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
        # a stale $SV2V that the slang frontend never uses does not block synthesis
        proc = subprocess.run([sys.executable, str(RUNNER), str(CORPUS / "inv.sv"), "--top", "inv", "-o",
                               str(self.tmp / "stale_sv2v"), "-q", "--frontend", "slang", "--no-equiv",
                               "--sim-cycles", "0"], capture_output=True, text=True, check=False,
                              env={**os.environ, "SV2V": str(not_exec)})
        m = json.loads((self.tmp / "stale_sv2v" / "metrics.json").read_text())
        self.assertEqual((proc.returncode, m["status"]), (0, "ok"), proc.stderr)
        self.assertNotIn("sv2v_version", m["tools"])

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

    def flow_with_slow_checks(self, out: Path, *extra: str, sim: dict | None = None) -> tuple[int, dict, dict]:
        """Run and2 in-process with every check (both proofs, the simulation) padded to take >= 0.4 s and its
        wall-clock window recorded; `sim` replaces the simulation result (the real one needs iverilog)."""
        real_yosys, real_sim, windows = bool_area.run_yosys, bool_area.run_diff_sim, {}

        def window(name: str, fn, *a, **kw):
            t0 = time.monotonic()
            res = fn(*a, **kw)
            time.sleep(max(0.0, 0.4 - (time.monotonic() - t0)))
            windows[name] = (t0, time.monotonic())
            return res

        def fake_yosys(yosys: str, script: str, script_path: Path, log_path: Path, timeout: int):
            if not script_path.name.startswith("equiv_"):
                return real_yosys(yosys, script, script_path, log_path, timeout)
            return window(script_path.stem.removeprefix("equiv_"), real_yosys, yosys, script, script_path, log_path,
                          timeout)

        def fake_sim(**kw):
            if sim is None:
                return window("simulation", real_sim, **kw)
            return window("simulation", lambda: dict(sim, work_dir=str(kw["work"])))

        with mock.patch.object(bool_area, "run_yosys", fake_yosys), mock.patch.object(bool_area, "run_diff_sim", fake_sim):
            code = bool_area.main([str(CORPUS / "and2.sv"), "--top", "and2", "-o", str(out), "-q", "--sim-cycles", "20",
                                   *extra])
        return code, json.loads((out / "metrics.json").read_text()), windows

    @unittest.skipUnless(IVERILOG, "the simulation check is only scheduled when iverilog is on PATH")
    def test_checks_run_concurrently_and_are_merged_in_a_fixed_order(self) -> None:
        stub = {"status": "match", "cycles": 20, "compared_bits": 20, "mismatches": 0, "gate_x_bits": 0}
        code3, m3, w3 = self.flow_with_slow_checks(self.tmp / "par", sim=stub)
        code1, m1, w1 = self.flow_with_slow_checks(self.tmp / "seq", "--check-jobs", "1", sim=stub)
        self.assertEqual((code3, code1), (0, 0), (m3["errors"], m1["errors"]))
        self.assertEqual(set(w3), {"rtl_vs_graph", "graph_vs_mapped", "simulation"})
        self.assertEqual(set(w1), set(w3))
        # all three windows overlap by default; with --check-jobs 1 none do
        self.assertLess(max(t0 for t0, _ in w3.values()), min(t1 for _, t1 in w3.values()), w3)
        for a, b in itertools.combinations(sorted(w1.values()), 2):
            self.assertLessEqual(a[1], b[0], w1)
        self.assertEqual((m3["timing"]["check_jobs"], m1["timing"]["check_jobs"]), (3, 1))
        self.assertLess(m3["timing"]["checks_seconds"], m1["timing"]["checks_seconds"])
        self.assertGreaterEqual(m1["timing"]["checks_seconds"], 1.2)
        # each check owns its own files: both proof scripts/logs and the sim directory exist side by side
        for name in ("equiv_rtl_vs_graph.ys", "equiv_rtl_vs_graph.log", "equiv_graph_vs_mapped.ys",
                     "equiv_graph_vs_mapped.log"):
            self.assertTrue((self.tmp / "par" / name).is_file(), name)
        self.assertEqual(m3["simulation"]["work_dir"], str(self.tmp / "par" / "sim"))
        # the same metrics.json, in the same order, apart from timing and where the run lives
        self.assertEqual(list(m3["equivalence"]["checks"]), ["rtl_vs_graph", "graph_vs_mapped"])
        self.assertEqual(list(m3), list(m1))
        self.assertEqual(m3["simulation"]["status"], "match")
        for m in (m3, m1):
            m.pop("timing"), m.pop("wall_seconds"), m.pop("artifacts")
            m["simulation"].pop("work_dir")
            for chk in m["equivalence"]["checks"].values():
                chk.pop("seconds"), chk.pop("log")
        self.assertEqual(json.dumps(m3), json.dumps(m1))

    @unittest.skipUnless(IVERILOG, "the simulation check is only scheduled when iverilog is on PATH")
    def test_check_failures_are_reported_in_a_fixed_order(self) -> None:
        # the simulation finishes long before the proofs and mismatches, yet the run is triaged as an equivalence
        # failure (proofs are the gate) and the simulation result is still recorded
        real = bool_area.run_yosys

        def broken_rtl_proof(yosys: str, script: str, script_path: Path, log_path: Path, timeout: int):
            if script_path.name == "equiv_rtl_vs_graph.ys":
                script_path.write_text(script)
                log_path.write_text("Found 1 $equiv cells in 1 modules.\n  Of those cells 0 are proven and 1 are unproven.\n"
                                    "ERROR: Found 1 unproven $equiv cells!\n")
                return False, log_path.read_text(), 0.0
            return real(yosys, script, script_path, log_path, timeout)

        bad = {"status": "mismatch", "cycles": 20, "compared_bits": 20, "mismatches": 3, "gate_x_bits": 0,
               "first_mismatches": ["cycle 2 y: rtl=1 gate=0"]}
        with mock.patch.object(bool_area, "run_yosys", broken_rtl_proof):
            code, m, _ = self.flow_with_slow_checks(self.tmp / "fail", "--equiv-bmc", "0", sim=bad)
        self.assertEqual((code, m["status"], m["stage"]), (1, "failed", "equivalence"))
        self.assertIn("rtl_vs_graph", m["errors"][0])
        self.assertEqual(m["equivalence"]["status"], "failed")
        self.assertEqual(m["equivalence"]["checks"]["graph_vs_mapped"]["status"], "proven")
        self.assertEqual(m["simulation"]["status"], "mismatch")
        # with the proofs passing, the simulation mismatch is what fails the run
        code, m, _ = self.flow_with_slow_checks(self.tmp / "simfail", sim=bad)
        self.assertEqual((code, m["stage"], m["equivalence"]["status"]), (1, "simulation", "proven"))
        self.assertIn("cycle 2 y", m["errors"][0])

    def test_check_jobs_is_validated(self) -> None:
        code, _, err = run_flow(self.tmp / "bad", "and2", [CORPUS / "and2.sv"], "--check-jobs", "0")
        self.assertEqual(code, 2)
        self.assertIn("--check-jobs must be at least 1", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
