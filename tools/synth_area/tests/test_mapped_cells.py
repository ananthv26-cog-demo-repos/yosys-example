#!/usr/bin/env python3
"""
Tests for mapped_cells.py and run_report.py: unit tests on a hand-written Liberty snippet and
write_json document (no yosys needed) and end-to-end checks of the mapped_cells.json,
run_manifest.json and summary.md that bool_area.py writes.

    python3 tools/synth_area/tests/test_mapped_cells.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
CORPUS = TOOL / "corpus"
sys.path.insert(0, str(TOOL))
import boolean_graph
import mapped_cells
import run_report

LIBERTY = """
library (tiny) {
  cell (INVx1) {
    area : 0.04374;
    pg_pin (VDD) { direction : input; pg_type : primary_power; }
    pin (A) { direction : input; capacitance : 0.1; }
    pin (Y) { direction : output; function : "!A"; }
  }
  cell ("NAND2x1") {
    area : 0.08748;
    pin (A) { direction : input; }
    pin (B) { direction : input; }
    pin (Y) { direction : output; function : "!(A * B)"; }
  }
  cell (DFF) {
    area : 0.3645;
    pin (CLK) { direction : input; clock : true; }
    pin (D) { direction : input; }
    pin (Q) { direction : output; function : "IQ"; }
  }
  cell (NOAREA) {
    pin (A) { direction : input; }
  }
}
"""

# a[0..1]=2,3 clk=4 q=5 n1=6 n2=7 ; q is also aliased by the hidden name $auto$q
MODULE = {
    "creator": "test",
    "modules": {
        "top": {
            "ports": {"a": {"direction": "input", "bits": [2, 3]}, "clk": {"direction": "input", "bits": [4]},
                      "q": {"direction": "output", "bits": [5]}},
            "cells": {
                "$abc$1$nand": {"type": "NAND2x1", "connections": {"A": [2], "B": [3], "Y": [6]}},
                "$abc$1$inv": {"type": "INVx1", "connections": {"A": [6], "Y": [7]},
                               "attributes": {"src": "top.sv:4.5-4.9"}},
                "$auto$dff": {"type": "DFF", "connections": {"CLK": [4], "D": [7], "Q": [5]}},
            },
            "netnames": {
                "a": {"bits": [2, 3], "hide_name": 0}, "clk": {"bits": [4], "hide_name": 0},
                "q": {"bits": [5], "hide_name": 0}, "$auto$q": {"bits": [5], "hide_name": 1},
                "$abc$1$n1": {"bits": [6], "hide_name": 1}, "$abc$1$n2": {"bits": [7], "hide_name": 1},
            },
        }
    },
}


class LibertyScanTests(unittest.TestCase):
    def test_area_pins_functions_and_power_pins(self) -> None:
        lib = mapped_cells.parse_liberty(LIBERTY)
        self.assertEqual(lib["INVx1"]["area"], 0.04374)
        self.assertEqual(lib["INVx1"]["pins"], {"A": {"direction": "input", "function": None},
                                                "Y": {"direction": "output", "function": "!A"}})
        self.assertEqual(lib["NAND2x1"]["pins"]["Y"]["function"], "!(A * B)", "quoted cell names are unquoted")
        self.assertNotIn("VDD", lib["INVx1"]["pins"])
        self.assertIsNone(lib["NOAREA"]["area"])

    def test_only_restricts_the_scan(self) -> None:
        self.assertEqual(set(mapped_cells.parse_liberty(LIBERTY, only={"DFF"})), {"DFF"})

    def test_gzip_is_transparent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tiny.lib.gz"
            p.write_bytes(gzip.compress(LIBERTY.encode()))
            self.assertEqual(mapped_cells.load_liberty([p])["DFF"]["area"], 0.3645)


class BuildCellsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = mapped_cells.parse_liberty(LIBERTY)

    def test_pins_nets_ordering_and_totals(self) -> None:
        r = mapped_cells.build_cells(MODULE, "top", self.lib)
        self.assertEqual(r["top"], "top")
        self.assertEqual([c["id"] for c in r["cells"]], ["cell0000", "cell0001", "cell0002"])
        self.assertEqual([c["type"] for c in r["cells"]], ["DFF", "INVx1", "NAND2x1"], "sorted by type")
        dff, inv, nand = r["cells"]
        self.assertEqual(dff["pins"], {
            "CLK": {"direction": "input", "net": "clk"}, "D": {"direction": "input", "net": "$abc$1$n2"},
            "Q": {"direction": "output", "net": "q", "function": "IQ"},
        })
        self.assertEqual(nand["pins"]["A"]["net"], "a[0]")
        self.assertEqual(nand["pins"]["B"]["net"], "a[1]")
        self.assertEqual(inv["src"], "top.sv:4.5-4.9")
        self.assertIsNone(nand["src"])
        self.assertEqual(r["summary"]["cells"], 3)
        self.assertEqual(r["summary"]["pins"], 8)
        self.assertAlmostEqual(r["summary"]["area"], 0.04374 + 0.08748 + 0.3645)
        self.assertEqual(list(r["summary"]["by_type"]), ["DFF", "INVx1", "NAND2x1"])
        self.assertEqual(r["summary"]["by_type"]["DFF"], {"count": 1, "area_each": 0.3645, "area": 0.3645})
        self.assertEqual(r["ports"]["a"], {"direction": "input", "width": 2})

    def test_net_names_join_the_boolean_graph(self) -> None:
        """The DFF's Q pin must carry the same label the Boolean graph gives that flop's node."""
        r = mapped_cells.build_cells(MODULE, "top", self.lib)
        graph = boolean_graph.build_graph(
            {"modules": {"top": {**MODULE["modules"]["top"], "cells": {
                "$auto$dff": {"type": "$_DFF_P_", "connections": {"C": [4], "D": [7], "Q": [5]}}}}}},
            "top", dff_types=["$_DFF_P_"])
        (dff_node,) = [n["name"] for n in graph["nodes"] if n["kind"] == "DFF"]
        self.assertEqual(r["cells"][0]["pins"]["Q"]["net"], dff_node)

    def test_deterministic_regardless_of_cell_order(self) -> None:
        shuffled = json.loads(json.dumps(MODULE))
        cells = shuffled["modules"]["top"]["cells"]
        shuffled["modules"]["top"]["cells"] = dict(reversed(list(cells.items())))
        self.assertEqual(mapped_cells.build_cells(MODULE, "top", self.lib),
                         mapped_cells.build_cells(shuffled, "top", self.lib))

    def test_unknown_cell_type_is_an_error_not_a_zero(self) -> None:
        for lib in (self.lib, {k: v for k, v in self.lib.items() if k != "DFF"}):
            doc = json.loads(json.dumps(MODULE))
            doc["modules"]["top"]["cells"]["$auto$dff"]["type"] = "NOAREA" if lib is self.lib else "DFF"
            with self.assertRaises(boolean_graph.UnsupportedCell) as cm:
                mapped_cells.build_cells(doc, "top", lib)
            self.assertIn("x1", str(cm.exception))

    def test_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "lib.lib").write_text(LIBERTY)
            (td / "m.json").write_text(json.dumps(MODULE))
            proc = subprocess.run([sys.executable, str(TOOL / "mapped_cells.py"), str(td / "m.json"), "--top", "top",
                                   "--liberty", str(td / "lib.lib"), "-o", str(td / "out.json")],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads((td / "out.json").read_text())["summary"]["cells"], 3)
            bad = subprocess.run([sys.executable, str(TOOL / "mapped_cells.py"), str(td / "m.json"), "--top", "nope",
                                  "--liberty", str(td / "lib.lib"), "-o", str(td / "out2.json")],
                                 capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("[mapped_cells] error", bad.stderr)


# a run that failed in mapping: word level exists, nothing below it
FAILED_METRICS = {
    "top": "t", "status": "failed", "stage": "mapping", "wall_seconds": 1.5,
    "sources": [{"path": "/x/t.sv", "sha256": "ab" * 32}],
    "profile": {"name": "p", "version": 2, "sha256": "cd" * 32, "liberty": [], "liberty_verified": True},
    "frontend": {"name": "slang"}, "tools": {"yosys": "/y", "yosys_version": "Yosys 0.69"},
    "word_level": {"operations": 2, "by_kind": {"ADD": 1, "REG": 1}, "register_bits": 4, "memory_bits": 0,
                   "input_bits": 5, "output_bits": 4},
    "sequential": None, "boolean": None, "mapped": None, "equivalence": None, "simulation": None,
    "timing": {"word_seconds": 0.1}, "warnings": ["w1"], "errors": ["boom"], "artifacts": {"word": "/o/word_level.json"},
}
VERSIONS = {"word_level": 1, "sequential_overlay": 1, "boolean_graph": 1, "mapped_cells": 1, "metrics": 2}


class RunReportTests(unittest.TestCase):
    def test_manifest_records_present_and_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = {k: Path(td) / f"{k}.json" for k in ("word", "sequential", "graph", "mapped_cells", "metrics", "manifest")}
            out["word"].write_text("{}\n")
            out["metrics"].write_text("{}\n")
            man = run_report.build_manifest(FAILED_METRICS, out, ["bool_area.py", "t.sv"], VERSIONS)
            self.assertEqual(man["schema_version"], run_report.MANIFEST_SCHEMA_VERSION)
            self.assertEqual((man["status"], man["stage"], man["errors"]), ("failed", "mapping", ["boom"]))
            self.assertEqual(man["generated_by"]["argv"], ["bool_area.py", "t.sv"])
            self.assertEqual(man["generated_by"]["yosys_version"], "Yosys 0.69")
            self.assertNotIn("manifest", man["artifacts"], "the manifest does not hash itself")
            self.assertEqual(man["artifacts"]["word"],
                             {"path": str(out["word"]), "exists": True, "bytes": 3,
                              "sha256": hashlib.sha256(b"{}\n").hexdigest()})
            self.assertEqual(man["artifacts"]["graph"], {"path": str(out["graph"]), "exists": False})
            self.assertEqual(man["layers"]["word_level"], {"file": "word.json", "schema_version": 1, "present": True})
            self.assertFalse(man["layers"]["mapped_cells"]["present"])
            self.assertEqual(man["layers"]["metrics"]["schema_version"], 2)

    def test_summary_renders_what_exists_and_the_errors(self) -> None:
        text = run_report.render_summary(FAILED_METRICS)
        self.assertIn("# t — design-layer summary", text)
        self.assertIn("status: **failed** (stage `mapping`)", text)
        self.assertIn("2 operations: ADD 1, REG 1", text)
        self.assertNotIn("## Boolean", text)
        self.assertNotIn("## Verification", text)
        self.assertIn("## Warnings\n\n- w1", text)
        self.assertIn("## Errors\n\n- boom", text)
        self.assertIn("- `word_level.json`", text)


class MappedCellsFlowTests(unittest.TestCase):
    """bool_area.py writes mapped_cells.json, run_manifest.json and summary.md; they agree with metrics.json."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="mapped_cells_test_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def flow(self, name: str, top: str, src: Path) -> tuple[Path, dict]:
        out = self.tmp / name
        proc = subprocess.run(
            [sys.executable, str(TOOL / "bool_area.py"), str(src), "--top", top, "-o", str(out), "-q", "--no-equiv",
             "--sim-cycles", "0"], capture_output=True, text=True, check=False,
        )
        metrics = json.loads((out / "metrics.json").read_text())
        self.assertEqual(proc.returncode, 0, metrics.get("errors", proc.stderr))
        return out, metrics

    def test_counter_cells_pins_manifest_summary(self) -> None:
        out, m = self.flow("cnt", "counter8_en", CORPUS / "counter8_en.sv")
        cells = json.loads((out / "mapped_cells.json").read_text())
        self.assertEqual(cells["summary"]["cells"], m["mapped"]["num_cells"])
        self.assertAlmostEqual(cells["summary"]["area"], m["mapped"]["area"], places=5)
        self.assertEqual({t: g["count"] for t, g in cells["summary"]["by_type"].items()}, m["mapped"]["cells_by_type"])
        self.assertEqual(m["mapped"]["pin_connections"], cells["summary"]["pins"])
        flops = [c for c in cells["cells"] if c["type"].startswith("DFF")]
        self.assertEqual(sorted(c["pins"]["Q"]["net"] for c in flops), [f"count[{i}]" for i in range(8)])
        self.assertTrue(all(c["pins"]["CLK"]["net"] == "clk" for c in flops))
        self.assertTrue(all(c["src"] and c["src"].startswith(str(CORPUS / "counter8_en.sv")) for c in flops),
                        "flops keep their RTL source location through mapping")
        for c in cells["cells"]:
            outs = [p for p in c["pins"].values() if p["direction"] == "output"]
            self.assertEqual(len(outs), 1, c)
            self.assertIn("function", outs[0])

        man = json.loads((out / "run_manifest.json").read_text())
        self.assertEqual((man["status"], man["top"]), ("ok", "counter8_en"))
        self.assertEqual(man["profile"], m["profile"])
        self.assertEqual(man["sources"], m["sources"])
        for key in ("word", "sequential", "graph", "mapped_cells", "metrics", "summary", "netlist"):
            entry = man["artifacts"][key]
            self.assertTrue(entry["exists"], key)
            self.assertEqual(entry["sha256"], hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest(), key)
        self.assertTrue(all(layer["present"] for layer in man["layers"].values()))

        text = (out / "summary.md").read_text()
        self.assertIn("status: **ok**", text)
        self.assertIn(f"area **{m['mapped']['area']} um^2**", text)
        self.assertIn("| `DFFHQx4_ASAP7_75t_R` | 8 |", text)
        self.assertIn("resets: `rst` sync active-high (8 bits)", text)

    def test_manifest_and_summary_are_written_on_failure(self) -> None:
        out = self.tmp / "bad"
        src = self.tmp / "bad.sv"
        src.write_text("module bad (input logic a, output logic y);\n  assign y = a &&& ;\nendmodule\n")
        proc = subprocess.run([sys.executable, str(TOOL / "bool_area.py"), str(src), "--top", "bad", "-o", str(out),
                               "-q", "--no-equiv", "--sim-cycles", "0"], capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        m = json.loads((out / "metrics.json").read_text())
        man = json.loads((out / "run_manifest.json").read_text())
        self.assertEqual((man["status"], man["stage"], man["errors"]), ("failed", m["stage"], m["errors"]))
        self.assertFalse(man["layers"]["mapped_cells"]["present"])
        self.assertIn("## Errors", (out / "summary.md").read_text())

    def test_unwritable_report_fails_the_run_and_the_other_reports_say_so(self) -> None:
        """A directory squatting on summary.md (or run_manifest.json) survives the output purge and makes
        that write fail: the exit code, metrics.json and whichever report still can be written all agree."""
        for blocked, other in (("summary.md", "run_manifest.json"), ("run_manifest.json", "summary.md")):
            out = self.tmp / f"blocked_{blocked}"
            (out / blocked).mkdir(parents=True)
            proc = subprocess.run([sys.executable, str(TOOL / "bool_area.py"), str(CORPUS / "inv.sv"), "--top", "inv",
                                   "-o", str(out), "-q", "--no-equiv", "--sim-cycles", "0"],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1, blocked)
            m = json.loads((out / "metrics.json").read_text())
            self.assertEqual((m["status"], m["stage"]), ("failed", "artifacts"))
            self.assertEqual(len(m["errors"]), 1, m["errors"])
            self.assertIn(f"could not write {blocked}", m["errors"][0])
            text = (out / other).read_text()
            self.assertIn("failed", text)
            self.assertIn(f"could not write {blocked}", text)
            if other == "run_manifest.json":
                man = json.loads(text)
                self.assertEqual(man["artifacts"]["summary"], {"path": str(out / "summary.md"), "exists": False})
                self.assertEqual(man["artifacts"]["metrics"]["sha256"],
                                 hashlib.sha256((out / "metrics.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
