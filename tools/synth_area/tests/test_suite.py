#!/usr/bin/env python3
"""
Corpus tests: hand-counted blocks through the full flow, manifest coverage, and the suite runner.

    python3 tools/synth_area/tests/test_suite.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
SUITE = TOOL / "run_suite.py"
CORPUS = TOOL / "corpus"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOL))
import suite_evidence
import synth_area
from test_bool_area import run_flow

YOSYS = synth_area.find_yosys(None)


@unittest.skipUnless(YOSYS, "yosys binary not found")
class HandCountTests(unittest.TestCase):
    def test_hand_counted_blocks(self) -> None:
        """Blocks with exactly one optimal Boolean structure must come out with the hand-counted gates."""
        cases = {
            "inv": {"not": 1, "gate_total": 1, "max_depth": 1, "mapped_cell_total": 1},
            "half_adder": {"and": 1, "xor": 1, "gate_total": 2, "max_depth": 1},
            "parity8": {"gate_total": 7, "max_depth": 3},
            "dff": {"dff": 1, "gate_total": 0, "max_depth": 0, "mapped_cell_total": 1},
            "shift_reg8": {"dff": 8, "gate_total": 0, "max_fanout": 1},
            "reg8_en": {"dff": 8, "mux": 8, "gate_total": 8, "max_depth": 1},
        }
        with tempfile.TemporaryDirectory(prefix="hand_count_") as td:
            for top, want in cases.items():
                code, m, err = run_flow(Path(td) / top, top, [CORPUS / f"{top}.sv"], "--sim-cycles", "0")
                self.assertEqual(code, 0, m.get("errors", err))
                self.assertEqual(m["status"], "ok")
                for k, v in want.items():
                    self.assertEqual(m["summary"][k], v, f"{top}.{k}")
                self.assertEqual(m["equivalence"]["status"], "proven", top)


@unittest.skipUnless(YOSYS, "yosys binary not found")
class SuiteRunnerTests(unittest.TestCase):
    def test_manifest_covers_minimum_corpus(self) -> None:
        m = json.loads((CORPUS / "suite.json").read_text())
        names = [b["name"] for b in m["blocks"]]
        self.assertGreaterEqual(len(names), 20)
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(sum("fifo" in n for n in names), 20)
        for b in m["blocks"]:
            for s in b["sources"]:
                self.assertTrue((TOOL / s).exists(), s)

    def test_suite_runner_checks_expectations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suite_test_") as td:
            tmp = Path(td)
            manifest = tmp / "suite.json"
            manifest.write_text(json.dumps({"blocks": [
                {"name": "inv", "sources": [str(CORPUS / "inv.sv")], "top": "inv",
                 "expect": {"not": 1, "gate_total": 1}},
                {"name": "and2_wrong", "sources": [str(CORPUS / "and2.sv")], "top": "and2",
                 "expect": {"gate_total": 99}},
            ]}))
            proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "out"), "--",
                                   "--sim-cycles", "0"], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            summary = json.loads((tmp / "out" / "suite_summary.json").read_text())
            by = {r["name"]: r for r in summary["results"]}
            self.assertTrue(by["inv"]["passed"])
            self.assertFalse(by["and2_wrong"]["passed"])
            self.assertEqual(by["and2_wrong"]["checks"][0]["got"], 1)
            table = (tmp / "out" / "suite_table.md").read_text()
            self.assertIn("| inv | PASS |", table)
            evidence = json.loads((tmp / "out" / "suite_evidence.json").read_text())
            by_id = {c["id"]: c for c in evidence["criteria"]}
            # the two-block throwaway manifest cannot satisfy the corpus-wide criteria, but the
            # per-run ones must hold and the layers must be there
            self.assertTrue(by_id["all_layers"]["ok"], by_id["all_layers"])
            self.assertTrue(by_id["asap7_area"]["ok"], by_id["asap7_area"])
            self.assertFalse(by_id["blocks_pass"]["ok"])
            self.assertFalse(by_id["fifo_variants"]["ok"])
            self.assertIsNone(by_id["determinism"]["ok"])
            # --sim-cycles 0 compared no bits, so the simulation criterion is unknown, not a pass
            self.assertIsNone(by_id["simulation"]["ok"], by_id["simulation"])
            self.assertTrue(all(b["artifacts"]["manifest"]["present"] for b in evidence["blocks"]))
            self.assertIn("PASS", (tmp / "out" / "EVIDENCE.md").read_text())
            # rerunning the same blocks against that evidence proves the layer files are reproducible
            proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "out2"),
                                   "--baseline", str(tmp / "out" / "suite_evidence.json"), "-j", "2", "--",
                                   "--sim-cycles", "0"], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)  # and2_wrong still fails its expectation
            rerun = json.loads((tmp / "out2" / "suite_evidence.json").read_text())
            det = {c["id"]: c for c in rerun["criteria"]}["determinism"]
            self.assertTrue(det["ok"], det)
            self.assertEqual([r["name"] for r in
                              json.loads((tmp / "out2" / "suite_summary.json").read_text())["results"]],
                             ["inv", "and2_wrong"], "-j must not reorder the results")
            # layer files that do not reproduce fail the run even when every block itself passed
            faked = json.loads((tmp / "out" / "suite_evidence.json").read_text())
            faked["blocks"][0]["artifacts"]["boolean_graph"]["sha256"] = "0" * 64
            (tmp / "faked.json").write_text(json.dumps(faked))
            inv_only = [sys.executable, str(SUITE), str(manifest), "--only", "inv", "-o", str(tmp / "out3")]
            proc = subprocess.run([*inv_only, "--baseline", str(tmp / "faked.json")],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("criterion FAILED: determinism", proc.stdout)
            # ... while the same selection without a baseline is a clean run
            self.assertEqual(subprocess.run(inv_only, capture_output=True, text=True, check=False).returncode, 0)
            # identical layer files prove nothing when the block was built from different inputs
            changed = json.loads((tmp / "out" / "suite_evidence.json").read_text())
            changed["blocks"][0]["inputs"]["profile"] = "0" * 64
            (tmp / "changed.json").write_text(json.dumps(changed))
            proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "--only", "inv", "-o",
                                   str(tmp / "out4"), "--baseline", str(tmp / "changed.json")],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("built from different sources, profile or yosys", proc.stdout)
            # an unusable baseline is a usage error before any block runs, not a traceback after all of them
            (tmp / "notevidence.json").write_text('{"blocks": 3}')
            # a `blocks` list alone is not enough: determinism indexes by name and reads nested dicts
            (tmp / "badblock.json").write_text(json.dumps(
                {"schema_version": suite_evidence.EVIDENCE_SCHEMA_VERSION, "blocks": [{"name": []}]}))
            (tmp / "oldschema.json").write_text(json.dumps({"schema_version": 1, "blocks": []}))
            for bad_baseline in (str(tmp / "typo.json"), str(tmp / "notevidence.json"),
                                 str(tmp / "badblock.json"), str(tmp / "oldschema.json")):
                proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "nobase"),
                                       "--baseline", bad_baseline], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("--baseline", proc.stderr)
                self.assertFalse((tmp / "nobase" / "suite_summary.json").exists())
            # duplicate names would have two concurrent blocks writing one output directory
            (tmp / "dupes.json").write_text(json.dumps({"blocks": [
                {"name": "inv", "sources": [str(CORPUS / "inv.sv")], "top": "inv"},
                {"name": "inv", "sources": [str(CORPUS / "and2.sv")], "top": "and2"}]}))
            proc = subprocess.run([sys.executable, str(SUITE), str(tmp / "dupes.json"), "-o", str(tmp / "dup"),
                                   "-j", "2"], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("duplicate block names in the manifest: inv", proc.stderr)
            # a selection with a name the manifest does not have is a usage error, even next to valid names
            for only in (["--only", "typo"], ["--only", "inv", "--only", "typo"]):
                proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "none"), *only],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("--only names not in the manifest: typo", proc.stderr)
                self.assertFalse((tmp / "none" / "suite_summary.json").exists())
            # a child that leaves a truncated, incomplete or mistyped metrics.json is a failed block, not an
            # aborted suite
            (tmp / "true_python").write_text("#!/bin/sh\nexit 0\n")
            (tmp / "true_python").chmod(0o755)
            for bad in ('{"status": "ok", "sta', '{"status": "failed"}', '["ok"]',
                        '{"status": "ok", "stage": null, "errors": [], "summary": "x"}',
                        '{"status": "failed", "stage": "mapping", "errors": [1], "summary": null}'):
                (tmp / "out" / "inv" / "metrics.json").write_text(bad)
                proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "out"), "--only",
                                       "inv", "--python", str(tmp / "true_python")],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                (r,) = json.loads((tmp / "out" / "suite_summary.json").read_text())["results"]
                self.assertEqual((r["passed"], r["status"]), (False, "bad-metrics"), bad)
                self.assertIn("unreadable", r["errors"][0])
            # a mistyped summary value is a failed expectation, not a TypeError
            (tmp / "max.json").write_text(json.dumps({"blocks": [
                {"name": "inv", "sources": [str(CORPUS / "inv.sv")], "top": "inv", "expect_max": {"gate_total": 5}}]}))
            (tmp / "out" / "inv" / "metrics.json").write_text(
                '{"status": "ok", "stage": null, "errors": [], "summary": {"gate_total": "x"}}')
            proc = subprocess.run([sys.executable, str(SUITE), str(tmp / "max.json"), "-o", str(tmp / "out"),
                                   "--python", str(tmp / "true_python")], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            (r,) = json.loads((tmp / "out" / "suite_summary.json").read_text())["results"]
            self.assertEqual((r["passed"], r["checks"][0]["got"], r["checks"][0]["passed"]), (False, "x", False))
            # a child that cannot even be launched is a failed block, the aggregates are still written, and the
            # block's artifacts from the earlier good run do not survive next to the failed result
            self.assertTrue((tmp / "out" / "inv" / "metrics.json").exists())
            proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "out"), "--only", "inv",
                                   "--python", str(tmp / "missing_python")], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            summary = json.loads((tmp / "out" / "suite_summary.json").read_text())
            (r,) = summary["results"]
            self.assertEqual((r["passed"], r["stage"]), (False, "launch"))
            self.assertIn("missing_python", r["errors"][0])
            self.assertIn("| inv | FAIL(launch) |", (tmp / "out" / "suite_table.md").read_text())
            self.assertFalse((tmp / "out" / "inv" / "metrics.json").exists())
            self.assertFalse((tmp / "out" / "inv" / "boolean_graph.json").exists())
            # when that cleanup itself fails, the launch error is still the first one reported
            if os.geteuid() != 0:
                inv = tmp / "out" / "inv"
                (inv / "metrics.json").write_text("{}")
                inv.chmod(0o555)
                try:
                    proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "out"), "--only",
                                           "inv", "--python", str(tmp / "missing_python")],
                                          capture_output=True, text=True, check=False)
                finally:
                    inv.chmod(0o755)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                (r,) = json.loads((tmp / "out" / "suite_summary.json").read_text())["results"]
                self.assertEqual(r["stage"], "launch")
                self.assertIn("missing_python", r["errors"][0])
                self.assertIn("stale artifacts", r["errors"][1])
                self.assertIn("| inv | FAIL(launch) |", (tmp / "out" / "suite_table.md").read_text())


class EvidenceAccountingTests(unittest.TestCase):
    """What the rollup is allowed to claim, on synthetic blocks: no yosys needed."""

    @staticmethod
    def block(name: str, **over: object) -> dict:
        b = {"name": name, "passed": True, "status": "ok", "seconds": 1, "errors": [],
             "artifacts": {layer: {"present": True, "sha256": "a" * 64} for layer in suite_evidence.LAYERS},
             "layers_present": True, "mapped": {"cells": 1, "area": 1.0},
             "equivalence": {"status": "proven", "checks": {}},
             "simulation": {"status": "match", "compared_bits": 8, "mismatches": 0},
             "inputs": {"sources": ["b" * 64], "profile": "c" * 64, "yosys_version": "Yosys 0.69+",
                        "top": name, "frontend": {"name": "slang", "defines": [], "include_dirs": []}},
             "src_coverage": {"with_src": dict.fromkeys(suite_evidence.LAYERS, 1),
                              "items": dict.fromkeys(suite_evidence.LAYERS, 1)}}
        return {**b, **over}

    def test_bounded_pairs_are_not_reported_as_proven(self) -> None:
        """A bounded fallback keeps the induction pass's unproven pairs; the rollup must say so."""
        bounded = self.block("fifo_shift", equivalence={
            "status": "bounded",
            "checks": {"rtl_vs_graph": {"status": "bounded", "proven": 14, "unproven": 24},
                       "graph_vs_mapped": {"status": "proven", "proven": 38, "unproven": 0}}})
        eq = {c["id"]: c for c in suite_evidence.criteria([bounded], None)}["equivalence"]
        self.assertIn("24 pair(s) settled by bounded check from reset", eq["measured"])
        self.assertIn("0 unproven pairs", eq["measured"])

    def test_missing_layer_files_do_not_match_a_baseline(self) -> None:
        gone = {layer: {"present": False, "sha256": None} for layer in suite_evidence.LAYERS}
        blocks = [self.block("inv", artifacts=gone, layers_present=False)]
        det = suite_evidence.determinism(blocks, {"blocks": blocks})
        self.assertFalse(det["ok"])
        self.assertEqual(len(det["differing"]), len(suite_evidence.LAYERS))

    def test_frontend_options_are_part_of_the_inputs(self) -> None:
        """Same sources and profile, different defines: the layer files are not a reproduction."""
        before = self.block("inv")
        after = json.loads(json.dumps(before))
        after["inputs"]["frontend"]["defines"] = ["UNUSED=1"]
        det = suite_evidence.determinism([after], {"blocks": [before]})
        self.assertFalse(det["ok"])
        self.assertEqual(det["changed_inputs"], ["inv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
