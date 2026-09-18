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
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
SUITE = TOOL / "run_suite.py"
CORPUS = TOOL / "corpus"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOL))
import bool_area
import suite_cache
import suite_evidence
import synth_area
from test_bool_area import run_flow

YOSYS = synth_area.find_yosys(None)


def fake(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def fake_yosys(path: Path, abc_default: str, share: Path | None = None) -> str:
    """A yosys stand-in that answers the two questions suite_cache asks the real one: `help abc` names
    `abc_default` as the -exe default, and reading a missing `+/` file reports the path under `share`."""
    lines = ["#!/bin/sh", f"echo '{path}'",
             f'echo \'        use the specified command instead of "{abc_default}" to execute ABC.\'']
    if share is not None:
        lines.append(f"echo \"ERROR: File \\`{share}/{suite_cache.SHARE_PROBE}' not found or is a directory\"")
    return fake(path, "\n".join(lines) + "\n")


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
            # two records under one name: whichever came last would silently be the one compared against
            dup = json.loads((tmp / "out" / "suite_evidence.json").read_text())
            dup["blocks"].append(dup["blocks"][0])
            (tmp / "dupbase.json").write_text(json.dumps(dup))
            for bad_baseline in (str(tmp / "typo.json"), str(tmp / "notevidence.json"),
                                 str(tmp / "badblock.json"), str(tmp / "oldschema.json"),
                                 str(tmp / "dupbase.json")):
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


@unittest.skipUnless(YOSYS, "yosys binary not found")
class SuiteCacheTests(unittest.TestCase):
    def test_unchanged_blocks_are_reused_and_any_changed_input_reruns(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suite_cache_") as td:
            tmp, out = Path(td), Path(td) / "out"
            for name in ("inv", "and2"):  # private copies so the sources can be edited
                (tmp / f"{name}.sv").write_text((CORPUS / f"{name}.sv").read_text())
            blocks = [{"name": "inv", "sources": [str(tmp / "inv.sv")], "top": "inv", "expect": {"gate_total": 1}},
                      {"name": "and2", "sources": [str(tmp / "and2.sv")], "top": "and2", "expect": {"gate_total": 1}}]
            manifest = tmp / "suite.json"
            manifest.write_text(json.dumps({"blocks": blocks}))

            def run(*more: str, extra: tuple[str, ...] = ("--sim-cycles", "0")) -> tuple[int, dict, str]:
                proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(out), "-j", "2", *more,
                                       "--", *extra], capture_output=True, text=True, check=False)
                self.assertNotIn("Traceback", proc.stderr)
                return proc.returncode, json.loads((out / "suite_summary.json").read_text()), proc.stdout

            def cached(summary: dict) -> dict[str, bool]:
                return {r["name"]: r["cached"] for r in summary["results"]}

            code, s1, _ = run()
            self.assertEqual(code, 0)
            self.assertEqual((cached(s1), s1["cached"]), ({"inv": False, "and2": False}, 0))
            self.assertTrue((out / "inv" / suite_cache.RECORD).is_file())
            graph_before = (out / "inv" / "boolean_graph.json").read_bytes()
            # nothing changed: both reused, results in manifest order, and every report says so
            code, s2, stdout = run()
            self.assertEqual(code, 0)
            self.assertEqual((cached(s2), s2["cached"]), ({"inv": True, "and2": True}, 2))
            self.assertEqual([r["name"] for r in s2["results"]], ["inv", "and2"])
            self.assertEqual(s2["results"][0]["summary"], s1["results"][0]["summary"])
            self.assertIn("2 cached", stdout)
            self.assertIn("| inv | PASS |", (out / "suite_table.md").read_text())
            self.assertIn("| cached |", (out / "suite_table.md").read_text())
            self.assertIn("2/2 blocks reused", (out / "EVIDENCE.md").read_text())
            self.assertTrue(all(b["cached"] for b in json.loads((out / "suite_evidence.json").read_text())["blocks"]))
            # a hand-count expectation is re-evaluated on a hit, so editing the manifest fails without a rerun
            blocks[1]["expect"] = {"gate_total": 99}
            manifest.write_text(json.dumps({"blocks": blocks}))
            code, s3, _ = run()
            self.assertEqual(code, 1)
            self.assertEqual(cached(s3), {"inv": True, "and2": True})
            self.assertFalse(s3["results"][1]["passed"])
            blocks[1]["expect"] = {"gate_total": 1}
            manifest.write_text(json.dumps({"blocks": blocks}))
            # an edited source reruns only its block
            (tmp / "and2.sv").write_text((tmp / "and2.sv").read_text() + "\n// edited\n")
            code, s4, _ = run()
            self.assertEqual((code, cached(s4)), (0, {"inv": True, "and2": False}))
            # a header found next to the source (no -I) is an input too: editing it reruns the block
            (tmp / "inv_cfg.svh").write_text("`define INV_CFG 1\n")
            (tmp / "inv.sv").write_text('`include "inv_cfg.svh"\n' + (tmp / "inv.sv").read_text())
            code, s4b, _ = run()
            self.assertEqual((code, cached(s4b)), (0, {"inv": False, "and2": True}))
            data = json.loads((out / "inv" / suite_cache.RECORD).read_text())
            self.assertTrue(data["fingerprint"]["inputs"][str(tmp / "inv_cfg.svh")])
            self.assertTrue(data["fingerprint"]["yosys_share"]["techmap.v"])
            (tmp / "inv_cfg.svh").write_text("`define INV_CFG 2\n")
            code, s4c, _ = run()
            self.assertEqual((code, cached(s4c)), (0, {"inv": False, "and2": True}))
            code, s4d, _ = run()
            self.assertEqual((code, cached(s4d)), (0, {"inv": True, "and2": True}))
            # a missing or altered artifact is a miss, never served from the record
            (out / "inv" / "boolean_graph.json").unlink()
            code, s5, _ = run()
            self.assertEqual((code, cached(s5)), (0, {"inv": False, "and2": True}))
            self.assertEqual((out / "inv" / "boolean_graph.json").read_bytes(), graph_before)
            (out / "inv" / "metrics.json").write_text((out / "inv" / "metrics.json").read_text() + "\n")
            self.assertFalse(suite_cache.is_hit(out / "inv", ["x"], sys.executable))
            code, s6, _ = run()
            self.assertEqual(cached(s6), {"inv": False, "and2": True})
            # outputs found by glob (sim/, equiv_*) and the binaries the run resolved are part of the record
            code, s6b, _ = run(extra=())
            self.assertEqual((code, cached(s6b)), (0, {"inv": False, "and2": False}))
            data = json.loads((out / "inv" / suite_cache.RECORD).read_text())
            self.assertTrue(data["artifacts"]["sim/sim.log"] and data["artifacts"]["equiv_rtl_vs_graph.log"])
            self.assertIsNone(data["artifacts"]["sv2v_out.v"])
            self.assertTrue(all(data["fingerprint"]["tools"][t] for t in ("python", "yosys", "abc", "iverilog", "vvp")))
            run_manifest = json.loads((out / "inv" / "run_manifest.json").read_text())
            self.assertEqual(run_manifest["generated_by"]["abc"], str(Path(YOSYS).resolve().with_name("yosys-abc")))
            (out / "inv" / "sim" / "sim.log").unlink()
            code, s6c, _ = run(extra=())
            self.assertEqual((code, cached(s6c)), (0, {"inv": False, "and2": True}))
            (out / "and2" / "equiv_graph_vs_mapped.log").unlink()
            code, s6d, _ = run(extra=())
            self.assertEqual((code, cached(s6d)), (0, {"inv": True, "and2": False}))
            self.assertTrue((out / "and2" / "equiv_graph_vs_mapped.log").is_file())
            # a run_manifest.json that parses but has the wrong shape is a miss, not a crash
            (out / "inv" / "run_manifest.json").write_text('{"sources": 3, "profile": [], "frontend": {"include_dirs": 7}}')
            code, s6e, _ = run(extra=())
            self.assertEqual((code, cached(s6e)), (0, {"inv": False, "and2": True}))
            self.assertIsInstance(json.loads((out / "inv" / "run_manifest.json").read_text())["sources"], list)
            # different flow options, --no-cache, and --baseline all rerun
            code, s7, _ = run(extra=("--sim-cycles", "0", "--equiv-seq", "3"))
            self.assertEqual((code, cached(s7)), (0, {"inv": False, "and2": False}))
            code, s8, _ = run("--no-cache", extra=("--sim-cycles", "0", "--equiv-seq", "3"))
            self.assertEqual((code, cached(s8)), (0, {"inv": False, "and2": False}))
            code, s9, _ = run("--baseline", str(out / "suite_evidence.json"),
                              extra=("--sim-cycles", "0", "--equiv-seq", "3"))
            self.assertEqual((code, cached(s9)), (0, {"inv": False, "and2": False}))
            self.assertTrue({c["id"]: c for c in json.loads((out / "suite_evidence.json").read_text())["criteria"]}
                            ["determinism"]["ok"])
            # the record is dropped before a rerun starts, so an interrupted run cannot leave a stale hit
            argv = [str(TOOL / "bool_area.py"), str(tmp / "inv.sv"), "--top", "inv", "-o", str(out / "inv"), "-q",
                    "--sim-cycles", "0", "--equiv-seq", "3"]
            self.assertTrue(suite_cache.is_hit(out / "inv", argv, sys.executable))
            self.assertTrue(suite_cache.forget(out / "inv"))
            self.assertFalse(suite_cache.is_hit(out / "inv", argv, sys.executable))
            # recorded only if the inputs fingerprinted before the launch are what the finished run reports
            before = suite_cache.fingerprint(argv, sys.executable, suite_cache.planned_manifest([str(tmp / "inv.sv")], argv))
            self.assertEqual(before, suite_cache.fingerprint(argv, sys.executable,
                                                             json.loads((out / "inv" / "run_manifest.json").read_text())))
            for stale in (None, {**before, "inputs": {**before["inputs"], str(tmp / "inv.sv"): "0" * 64}}):
                self.assertFalse(suite_cache.record(out / "inv", argv, sys.executable, stale))
                self.assertFalse((out / "inv" / suite_cache.RECORD).exists())
            self.assertTrue(suite_cache.record(out / "inv", argv, sys.executable, before))
            self.assertTrue(suite_cache.is_hit(out / "inv", argv, sys.executable))
            # a source edited while its block runs (here: by the interpreter wrapper, before bool_area starts)
            # is not recorded, whichever contents the run read; the next run redoes it and then it caches
            (tmp / "editing_python").write_text(f"#!/bin/sh\necho '// edited mid-run' >> '{tmp / 'inv.sv'}'\n"
                                                f"exec '{sys.executable}' \"$@\"\n")
            (tmp / "editing_python").chmod(0o755)
            proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(out), "--only", "inv",
                                   "--python", str(tmp / "editing_python"), "--", "--sim-cycles", "0", "--equiv-seq", "3"],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(json.loads((out / "inv" / "metrics.json").read_text())["status"], "ok")
            self.assertIn("edited mid-run", (tmp / "inv.sv").read_text())
            self.assertFalse((out / "inv" / suite_cache.RECORD).exists())
            code, s9b, _ = run(extra=("--sim-cycles", "0", "--equiv-seq", "3"))
            self.assertEqual((code, cached(s9b)), (0, {"inv": False, "and2": True}))
            code, s9c, _ = run(extra=("--sim-cycles", "0", "--equiv-seq", "3"))
            self.assertEqual((code, cached(s9c)), (0, {"inv": True, "and2": True}))
            self.assertTrue(suite_cache.is_hit(out / "inv", argv, sys.executable))
            # a record whose tool-code, tool-binary, yosys share file or interpreter hash differs is a miss
            data = json.loads((out / "inv" / suite_cache.RECORD).read_text())
            for path, value in (("code", {"bool_area.py": "0" * 64}), ("tools", {"yosys": "0" * 64}),
                                ("tools", {"python": "0" * 64}), ("tools", {"sv2v": "0" * 64}),
                                ("tools", {"abc": "0" * 64}), ("yosys_share", {"techmap.v": "0" * 64}),
                                ("python", "/p")):
                edited = json.loads(json.dumps(data))
                if isinstance(value, dict):
                    edited["fingerprint"][path].update(value)
                else:
                    edited["fingerprint"][path] = value
                (out / "inv" / suite_cache.RECORD).write_text(json.dumps(edited))
                self.assertFalse(suite_cache.is_hit(out / "inv", argv, sys.executable), path)
            # a failed run is never recorded
            (tmp / "true_python").write_text("#!/bin/sh\nexit 0\n")
            (tmp / "true_python").chmod(0o755)
            (out / "inv" / "metrics.json").write_text('{"status": "failed", "stage": "mapping", "errors": ["boom"]}')
            proc = subprocess.run([sys.executable, str(SUITE), str(manifest), "-o", str(out), "--only", "inv",
                                   "--python", str(tmp / "true_python")], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertFalse((out / "inv" / suite_cache.RECORD).exists())
            # something in the record's way (a directory of that name) is not a hit, and a rerun still starts
            (out / "inv" / suite_cache.RECORD).mkdir()
            self.assertFalse(suite_cache.is_hit(out / "inv", argv, sys.executable))
            self.assertFalse(suite_cache.forget(out / "inv"))
            code, s10, _ = run(extra=("--sim-cycles", "0", "--equiv-seq", "3"))
            self.assertEqual((code, cached(s10)), (0, {"inv": False, "and2": True}))
            self.assertTrue((out / "inv" / suite_cache.RECORD).is_dir())


class CacheFingerprintTests(unittest.TestCase):
    """Fingerprint pieces that need no yosys run."""

    def test_included_files_are_inputs(self) -> None:
        """`include is resolved next to the including file, then along -I, transitively; unresolved -> None."""
        with tempfile.TemporaryDirectory(prefix="suite_incf_") as td:
            src, inc = Path(td) / "src", Path(td) / "inc"
            src.mkdir(), inc.mkdir()
            (src / "top.sv").write_text('`include "local.svh"\n  `include <shared.svh>\n'
                                        '// `include "commented.svh" is not an include line\n'
                                        '`ifdef NEVER\n`include "missing.svh"\n`endif\n')
            # a file name only the preprocessor knows can never be hashed: the block is not cacheable
            (src / "macro.sv").write_text('`define HEADER "local.svh"\n`include `HEADER\n')
            found = suite_cache.include_files([str(src / "macro.sv")], [])
            self.assertEqual(found, {f'{src / "macro.sv"}: `include `HEADER': None})
            fp = suite_cache.fingerprint(["x"], sys.executable, {"sources": [{"path": str(src / "macro.sv")}]})
            self.assertFalse(suite_cache.complete({"fingerprint": fp, "artifacts": {}}))
            (src / "local.svh").write_text('`include "local.svh"\n`include "shared.svh"\n')  # self-include: terminates
            (inc / "shared.svh").write_text("`define S 1\n")
            found = suite_cache.include_files([str(src / "top.sv")], [str(inc)])
            self.assertEqual(sorted(found), [str(inc / "shared.svh"), str(src / "local.svh"),
                                             f'{src / "top.sv"}: `include missing.svh'])
            self.assertTrue(found[str(inc / "shared.svh")] and found[str(src / "local.svh")])
            self.assertIsNone(found[f'{src / "top.sv"}: `include missing.svh'])
            # the unresolved include keeps the block out of the cache; resolving it lets it back in
            manifest = {"sources": [{"path": str(src / "top.sv")}], "frontend": {"include_dirs": [str(inc)]}}
            fp = suite_cache.fingerprint(["x"], sys.executable, manifest)
            self.assertIsNone(fp["inputs"][f'{src / "top.sv"}: `include missing.svh'])
            (src / "missing.svh").write_text("")
            fp2 = suite_cache.fingerprint(["x"], sys.executable, manifest)
            self.assertTrue(fp2["inputs"][str(src / "missing.svh")])
            self.assertFalse([k for k in fp2["inputs"] if "`include" in k])
            # a header found beside the source wins over one of the same name on -I (as the frontends do)
            (src / "shared.svh").write_text("`define S 2\n")
            found = suite_cache.include_files([str(src / "top.sv")], [str(inc)])
            self.assertIn(str(src / "shared.svh"), found)
            self.assertNotIn(str(inc / "shared.svh"), found)
            self.assertEqual(suite_cache.include_files(["", str(src / "nope.sv")], []), {})

    def test_yosys_share_files_are_inputs(self) -> None:
        share = suite_cache.yosys_share_dir(YOSYS)
        self.assertTrue(share and (share / "techmap.v").is_file(), share)
        hashes = suite_cache.share_hashes(YOSYS)
        self.assertTrue(hashes["techmap.v"] and hashes["simcells.v"])
        with tempfile.TemporaryDirectory(prefix="suite_share_") as td:
            # the directory is whatever this yosys says it expands `+/` to, wherever that is
            share_dir = Path(td) / "opt" / "acme-yosys"
            share_dir.mkdir(parents=True)
            (share_dir / "techmap.v").write_text("// t\n")
            y = fake_yosys(Path(td) / "bin" / "yosys", bool_area.ABC_BUILTIN, share_dir)
            self.assertEqual(suite_cache.yosys_share_dir(y), share_dir)
            self.assertEqual(list(suite_cache.share_hashes(y)), ["techmap.v"])
            # one that names no directory, or one that is not there -> never cached
            for y in (fake(Path(td) / "mute" / "yosys", "#!/bin/sh\n"),
                      fake_yosys(Path(td) / "gone" / "yosys", bool_area.ABC_BUILTIN, Path(td) / "nowhere")):
                self.assertIsNone(suite_cache.yosys_share_dir(y))
                self.assertEqual(suite_cache.share_hashes(y), {"": None})
                fp = suite_cache.fingerprint(["--yosys", y], sys.executable, {})
                self.assertFalse(suite_cache.complete({"fingerprint": fp, "artifacts": {}}))

    def test_profile_script_files_are_inputs(self) -> None:
        """Files a profile's yosys commands name (`techmap -map x.v`) are hashed, a relative one from the
        block's output directory (yosys runs there); one that cannot be found keeps the profile's blocks
        out of the cache; placeholders, options and +/ share files are not files."""
        base = json.loads((TOOL / "profiles" / "asap7_rvt_tt_v1.json").read_text())
        self.assertEqual(suite_cache.script_files(base, None), {})
        with tempfile.TemporaryDirectory(prefix="suite_prof_") as td:
            mapping, block_out = Path(td) / "custom_map.v", Path(td) / "out" / "blk"
            mapping.write_text("// v1\n")
            profile = json.loads(json.dumps(base))
            profile["script"]["lower"][1:1] = [f"techmap -map {mapping}", "techmap -map +/techmap.v",
                                               'read_verilog -lib "lib/asap7/cells.v"', "tee -o {stat_txt} stat"]
            path = Path(td) / "custom.json"
            path.write_text(json.dumps(profile))
            found = suite_cache.script_files(profile, block_out)
            self.assertEqual(sorted(found), sorted(["lib/asap7/cells.v", str(mapping)]))
            self.assertTrue(found[str(mapping)])
            self.assertIsNone(found["lib/asap7/cells.v"])  # not under the block's output directory
            manifest = {"profile": {"path": str(path)}}
            argv = ["x", "-o", str(block_out)]
            fp = suite_cache.fingerprint(argv, sys.executable, manifest)
            self.assertFalse(suite_cache.complete({"fingerprint": fp, "artifacts": {}}))
            # put it where yosys would find it (relative to -o, as bool_area.py resolves it): now an input
            (block_out / "lib" / "asap7").mkdir(parents=True)
            (block_out / "lib" / "asap7" / "cells.v").write_text("// cells\n")
            fp = suite_cache.fingerprint(argv, sys.executable, manifest)
            self.assertTrue(fp["inputs"]["lib/asap7/cells.v"])
            for other in (["x", "-o", str(td)], ["x"]):  # another output directory, or none known
                self.assertIsNone(suite_cache.fingerprint(other, sys.executable, manifest)["inputs"]["lib/asap7/cells.v"])
            self.assertEqual(suite_cache.fingerprint(["x", f"--out-dir={block_out}"], sys.executable, manifest)["inputs"],
                             fp["inputs"])
            del profile["script"]["lower"][3]
            path.write_text(json.dumps(profile))
            fp = suite_cache.fingerprint(["x"], sys.executable, manifest)
            self.assertFalse([k for k in fp["inputs"] if k.startswith("lib/")])
            self.assertTrue(fp["inputs"][str(mapping)] and fp["inputs"][str(path)])
            mapping.write_text("// v2\n")
            fp2 = suite_cache.fingerprint(["x"], sys.executable, manifest)
            self.assertNotEqual(fp["inputs"][str(mapping)], fp2["inputs"][str(mapping)])

    def test_malformed_manifest_shapes_never_raise(self) -> None:
        for manifest in ({"sources": 3}, {"sources": [3, {"path": 4}]}, {"profile": {"path": 5}}, {"profile": 6},
                         {"frontend": {"include_dirs": "x"}}, {"frontend": {"include_dirs": [{}, None]}},
                         {"generated_by": []}, {"generated_by": {"yosys": 1, "abc": [], "sv2v": 2}},
                         {"sources": [{"path": "a\0b"}]}, {"profile": {"path": "a\0b"}},
                         {"frontend": {"include_dirs": ["a\0b"]}}):
            fp = suite_cache.fingerprint(["x"], sys.executable, manifest)
            self.assertFalse(suite_cache.complete({"fingerprint": fp or {}, "artifacts": {}}), manifest)
        # a NUL in the command line itself is a miss too, not a traceback
        for argv in (["--yosys", "a\0b"], ["--yosys", "/a\0b"], ["--yosys", "./a\0b"], ["--sv2v=./a\0b"], ["-o", "a\0b"]):
            fp = suite_cache.fingerprint(argv, sys.executable, {})
            self.assertFalse(suite_cache.complete({"fingerprint": fp or {}, "artifacts": {}}), argv)

    def test_include_dir_hashes_follow_directory_symlinks_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suite_inc_") as td:
            inc, target = Path(td) / "inc", Path(td) / "target"
            (inc / "sub").mkdir(parents=True)
            target.mkdir()
            (inc / "sub" / "a.svh").write_text("`define A 1\n")
            (target / "b.svh").write_text("`define B 1\n")
            (inc / "linked").symlink_to(target, target_is_directory=True)
            (inc / "loop").symlink_to(inc, target_is_directory=True)  # cycle: must terminate
            before = suite_cache.dir_hashes(inc)
            self.assertEqual(sorted(before), ["linked/b.svh", "sub/a.svh"])
            self.assertTrue(all(before.values()))
            (target / "b.svh").write_text("`define B 2\n")
            after = suite_cache.dir_hashes(inc)
            self.assertNotEqual(before["linked/b.svh"], after["linked/b.svh"])
            self.assertEqual(before["sub/a.svh"], after["sub/a.svh"])
            (target / "c.svh").write_text("")
            self.assertIn("linked/c.svh", suite_cache.dir_hashes(inc))
            self.assertEqual(suite_cache.dir_hashes(Path(td) / "missing"), {"": None})

    def test_tools_are_the_ones_a_run_today_would_use(self) -> None:
        """Tool hashes come from today's resolution (argv, $YOSYS/$SV2V/$ABC, PATH), not the old manifest."""
        with tempfile.TemporaryDirectory(prefix="suite_tools_") as td, \
                unittest.mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
            a, b = Path(td) / "a", Path(td) / "b"
            ya, yb = fake_yosys(a / "yosys", bool_area.ABC_BUILTIN), fake_yosys(b / "yosys", bool_area.ABC_BUILTIN)
            fake(a / "yosys-abc", "a"), fake(b / "yosys-abc", "b")
            py = sys.executable
            # explicit option, both spellings, last one wins
            self.assertEqual(suite_cache.tool_paths(["--yosys", ya], py)["yosys"], ya)
            self.assertEqual(suite_cache.tool_paths([f"--yosys={yb}"], py)["abc"], str(b / "yosys-abc"))
            self.assertEqual(suite_cache.tool_paths(["--yosys", ya, f"--yosys={yb}"], py)["yosys"], yb)
            # the environment selecting another binary changes the hash: that is the miss
            with unittest.mock.patch.dict(os.environ, {"YOSYS": ya}):
                fa = suite_cache.fingerprint(["x"], py, {})
            with unittest.mock.patch.dict(os.environ, {"YOSYS": yb}):
                fb = suite_cache.fingerprint(["x"], py, {})
            self.assertTrue(fa["tools"]["yosys"] and fb["tools"]["yosys"])
            self.assertNotEqual(fa["tools"]["yosys"], fb["tools"]["yosys"])
            self.assertNotEqual(fa["tools"]["abc"], fb["tools"]["abc"])
            # a build with the bundled ABC ignores $ABC (yosys does), and finds its sibling through symlinks
            ext = fake(Path(td) / "ext" / "abc", "ext")
            with unittest.mock.patch.dict(os.environ, {"ABC": ext}):
                self.assertEqual(bool_area.abc_executable(ya), str(a / "yosys-abc"))
            (Path(td) / "link").symlink_to(a / "yosys")
            self.assertEqual(bool_area.abc_executable(str(Path(td) / "link")), str(a / "yosys-abc"))
            # a build with YOSYS_PROGRAM_PREFIX names its bundled ABC `<yosys-bindir>/<prefix>yosys-abc`
            yp = fake_yosys(Path(td) / "p" / "acme-yosys", bool_area.ABC_BINDIR + "acme-yosys-abc")
            pabc = fake(Path(td) / "p" / "acme-yosys-abc", "p")
            self.assertEqual(bool_area.abc_executable(yp), pabc)
            self.assertEqual(suite_cache.tool_paths(["--yosys", yp], py)["abc"], pabc)
            # a build with an external ABC (ABCEXTERNAL) runs the compiled-in path, or $ABC when set; a
            # non-executable one hashes as None (never a hit)
            ext2 = fake(Path(td) / "ext" / "abc2", "ext2")
            ye = fake_yosys(Path(td) / "e" / "yosys", ext)
            fake(Path(td) / "e" / "yosys-abc", "unused sibling")
            self.assertEqual(bool_area.abc_executable(ye), ext)
            self.assertEqual(suite_cache.tool_paths(["--yosys", ye], py)["abc"], ext)
            with unittest.mock.patch.dict(os.environ, {"ABC": ext2}):
                self.assertEqual(bool_area.abc_executable(ye), ext2)
                self.assertEqual(suite_cache.tool_paths(["--yosys", ye], py)["abc"], ext2)
            with unittest.mock.patch.dict(os.environ, {"ABC": str(Path(td) / "nope")}):
                self.assertIsNone(suite_cache.tool_paths(["--yosys", ye], py)["abc"])
            # a yosys that will not say (no recognisable help text) -> no abc -> never cached
            yq = fake(Path(td) / "q" / "yosys", "#!/bin/sh\necho nothing\n")
            fake(Path(td) / "q" / "yosys-abc", "sibling")
            self.assertIsNone(bool_area.abc_executable(yq))
            fq = suite_cache.fingerprint(["--yosys", yq], py, {})
            self.assertIsNone(fq["tools"]["abc"])
            self.assertFalse(suite_cache.complete({"fingerprint": fq, "artifacts": {}}))
            # sv2v / iverilog / vvp: $SV2V and PATH as bool_area uses them; absent -> None
            self.assertIsNone(suite_cache.tool_paths([], py)["iverilog"])
            sv = fake(Path(td) / "tools" / "sv2v", "s")
            fake(Path(td) / "tools" / "iverilog", "i")
            with unittest.mock.patch.dict(os.environ, {"SV2V": sv, "PATH": str(Path(td) / "tools")}):
                paths = suite_cache.tool_paths([], py)
            self.assertEqual((paths["sv2v"], paths["iverilog"], paths["vvp"]),
                             (sv, str(Path(td) / "tools" / "iverilog"), None))
            self.assertEqual(suite_cache.tool_paths(["--sv2v", ya], py)["sv2v"], ya)
            # a non-executable yosys is None: the record can never be complete
            (a / "yosys").chmod(0o644)
            self.assertIsNone(suite_cache.tool_paths(["--yosys", ya], py)["yosys"])
        # a record without an abc hash can never hit
        fp = {"inputs": {"a": "0" * 64}, "tools": {"python": "1" * 64, "yosys": "2" * 64, "abc": None}}
        artifacts = {"metrics.json": "3" * 64, "run_manifest.json": "4" * 64}
        self.assertFalse(suite_cache.complete({"fingerprint": fp, "artifacts": artifacts}))
        fp["tools"]["abc"] = "5" * 64
        self.assertTrue(suite_cache.complete({"fingerprint": fp, "artifacts": artifacts}))


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
