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
        self.assertGreaterEqual(sum("fifo" in n for n in names), 8)
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
                        '{"status": "ok", "stage": null, "errors": [], "summary": "x"}'):
                (tmp / "out" / "inv" / "metrics.json").write_text(bad)
                proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(tmp / "out"), "--only",
                                       "inv", "--python", str(tmp / "true_python")],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                (r,) = json.loads((tmp / "out" / "suite_summary.json").read_text())["results"]
                self.assertEqual((r["passed"], r["status"]), (False, "bad-metrics"), bad)
                self.assertIn("unreadable", r["errors"][0])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
