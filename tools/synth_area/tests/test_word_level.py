#!/usr/bin/env python3
"""
Tests for word_level.py: unit tests on hand-built write_json documents (no yosys needed) and an
end-to-end check of the word_level.json that bool_area.py writes for the example FIFO.

    python3 tools/synth_area/tests/test_word_level.py
"""

from __future__ import annotations

import filecmp
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
CORPUS = TOOL / "corpus"
EXAMPLES = TOOL / "examples"
sys.path.insert(0, str(TOOL))
import boolean_graph
import synth_area
import word_level

YOSYS = synth_area.find_yosys(None)

# a[3:0] = bits 2..5, b[1:0] = bits 6..7, y[4:0] = bits 8..12, hidden alias of y, $auto temp = 13
NETNAMES = {
    "a": {"bits": [2, 3, 4, 5], "hide_name": 0},
    "b": {"bits": [6, 7], "hide_name": 0},
    "y": {"bits": [8, 9, 10, 11, 12], "hide_name": 0},
    "u.y_q": {"bits": [8, 9, 10, 11, 12], "hide_name": 0},
    "$auto$y": {"bits": [8, 9, 10, 11, 12], "hide_name": 1},
    "$1_Y": {"bits": [13], "hide_name": 1},
}


def fake_module(cells: dict, ports: dict | None = None, netnames: dict | None = None, memories: dict | None = None,
                attrs: dict | None = None) -> dict:
    mod = {"ports": ports or {}, "cells": cells, "netnames": NETNAMES if netnames is None else netnames}
    if memories:
        mod["memories"] = memories
    if attrs:
        mod["attributes"] = attrs
    return {"modules": {"top": mod}}


def cell(ctype: str, conns: dict, params: dict | None = None, src: str | None = None) -> dict:
    out = {"Y", "Q"} | ({"DATA"} if ctype.startswith("$memrd") else set())
    c = {"type": ctype, "connections": conns, "parameters": params or {},
         "port_directions": {p: ("output" if p in out else "input") for p in conns}}
    if src:
        c["attributes"] = {"src": src}
    return c


class SignalRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.names = word_level.bit_names({"netnames": NETNAMES})

    def render(self, bits: list) -> str:
        return word_level.render_signal(bits, self.names)

    def test_whole_wire_slice_and_bit(self) -> None:
        self.assertEqual(self.render([2, 3, 4, 5]), "a")
        self.assertEqual(self.render([3, 4]), "a[2:1]")
        self.assertEqual(self.render([5]), "a[3]")

    def test_public_shallow_name_beats_hidden_and_hierarchical_aliases(self) -> None:
        self.assertEqual(self.render([8, 9, 10, 11, 12]), "y")
        self.assertEqual(self.render([13]), "$1_Y", "hidden names are still better than nothing")
        self.assertEqual(self.render([99]), "$bit99", "a bit with no wire at all falls back to its id")

    def test_constants_and_concatenation_are_msb_first(self) -> None:
        self.assertEqual(self.render(["1", "0", "0"]), "3'b001")
        self.assertEqual(self.render([6, 7, "0"]), "{1'b0, b}")
        self.assertEqual(self.render([2, 3, 6, 7]), "{b, a[1:0]}")
        self.assertEqual(self.render([2, "x", 3]), "{a[1], 1'bx, a[0]}")

    def test_offset_and_upto_wires_keep_hdl_indices(self) -> None:
        names = word_level.bit_names({"netnames": {
            "v": {"bits": [2, 3, 4], "offset": 4},          # logic [6:4] v
            "w": {"bits": [5, 6, 7], "upto": 1},            # logic [0:2] w -> bits[0] is w[2]
        }})
        self.assertEqual(word_level.render_signal([2, 3, 4], names), "v")
        self.assertEqual(word_level.render_signal([3, 4], names), "v[6:5]")
        self.assertEqual(word_level.render_signal([5, 6, 7], names), "w")
        self.assertEqual(word_level.render_signal([7], names), "w[0]")
        self.assertEqual(word_level.render_signal([6, 7], names), "w[0:1]")


class HelperTests(unittest.TestCase):
    def test_param_value(self) -> None:
        self.assertEqual(word_level.param_value(5), 5)
        self.assertEqual(word_level.param_value("00000101"), 5)
        self.assertEqual(word_level.param_value("0x"), "0x")
        self.assertEqual(word_level.param_value("\\mem"), "\\mem")
        self.assertEqual(word_level.param_value("0101 "), "0101", "trailing space marks a text parameter")

    def test_normalize_src(self) -> None:
        base = Path("/runs/fifo")
        self.assertIsNone(word_level.normalize_src(None, base))
        self.assertIsNone(word_level.normalize_src("", base))
        self.assertEqual(word_level.normalize_src("../../src/f.sv:3.1-3.9", base), "/src/f.sv:3.1-3.9")
        self.assertEqual(word_level.normalize_src("/abs/f.sv:3.1", base), "/abs/f.sv:3.1")
        self.assertEqual(word_level.normalize_src("a.sv:1.1|../b.sv:2.2", base), "/runs/fifo/a.sv:1.1|/runs/b.sv:2.2")
        self.assertEqual(word_level.normalize_src("../f.sv:3.1", None), "../f.sv:3.1", "no base: untouched")

    def test_memory_name(self) -> None:
        self.assertEqual(word_level.memory_name("\\u.mem"), "u.mem")
        self.assertEqual(word_level.memory_name("$mem$1"), "$mem$1")
        self.assertIsNone(word_level.memory_name(None))


class ReportTests(unittest.TestCase):
    def test_add_operation_widths_signedness_and_src(self) -> None:
        data = fake_module({
            "$1": cell("$add", {"A": [2, 3, 4, 5, "0"], "B": [6, 7, "0", "0", "0"], "Y": [8, 9, 10, 11, 12]},
                       {"A_SIGNED": "0", "B_SIGNED": "1", "A_WIDTH": "101", "B_WIDTH": "101", "Y_WIDTH": "101"},
                       src="../f.sv:3.5-3.10"),
        }, ports={"a": {"direction": "input", "bits": [2, 3, 4, 5]}, "b": {"direction": "input", "bits": [6, 7]},
                  "y": {"direction": "output", "bits": [8, 9, 10, 11, 12]}}, attrs={"src": "../f.sv:1.1-9.10"})
        r = word_level.build_report(data, "top", src_base=Path("/runs/x"))
        self.assertEqual(r["schema_version"], word_level.WORD_SCHEMA_VERSION)
        self.assertEqual(r["src"], "/runs/f.sv:1.1-9.10")
        self.assertEqual(len(r["operations"]), 1)
        op = r["operations"][0]
        self.assertEqual((op["id"], op["kind"], op["category"], op["yosys_type"]), ("op0000", "ADD", "arithmetic", "$add"))
        self.assertEqual(op["width"], 5)
        self.assertEqual(op["signed"], {"A": False, "B": True})
        self.assertEqual(op["inputs"], {"A": {"width": 5, "signal": "{1'b0, a}"}, "B": {"width": 5, "signal": "{3'b000, b}"}})
        self.assertEqual(op["outputs"], {"Y": {"width": 5, "signal": "y"}})
        self.assertEqual(op["parameters"]["A_WIDTH"], 5)
        self.assertEqual(op["src"], "/runs/f.sv:3.5-3.10")
        self.assertEqual(r["summary"], {"operations": 1, "by_kind": {"ADD": 1}, "by_category": {"arithmetic": 1},
                                        "register_bits": 0, "memory_bits": 0, "input_bits": 6, "output_bits": 5})

    def test_registers_and_memory_ports(self) -> None:
        data = fake_module({
            "$r": cell("$adffe", {"CLK": [2], "ARST": [3], "EN": [4], "D": [6, 7], "Q": [8, 9]},
                       {"WIDTH": "10", "CLK_POLARITY": "1", "ARST_POLARITY": "0", "ARST_VALUE": "00", "EN_POLARITY": "1"}),
            "$w": cell("$memwr_v2", {"CLK": [2], "EN": [4, 4], "ADDR": [6, 7], "DATA": [8, 9]},
                       {"MEMID": "\\u.mem", "WIDTH": "10", "ABITS": "10"}),
            "$rd": cell("$memrd_v2", {"CLK": ["x"], "EN": ["1"], "ARST": ["0"], "SRST": ["0"], "ADDR": [6, 7],
                                      "DATA": [10, 11]}, {"MEMID": "\\u.mem", "WIDTH": "10", "ABITS": "10"}),
        }, memories={"u.mem": {"width": 2, "size": 4, "start_offset": 0, "attributes": {"src": "f.sv:7.1"}}})
        r = word_level.build_report(data, "top")
        for c in data["modules"]["top"]["cells"].values():
            del c["port_directions"]
        self.assertEqual(word_level.build_report(data, "top"), r, "pin directions fall back to RTLIL conventions")
        by_kind = {op["kind"]: op for op in r["operations"]}
        self.assertEqual(set(by_kind), {"REG", "MEMWR", "MEMRD"})
        self.assertEqual(by_kind["REG"]["width"], 2)
        self.assertEqual(by_kind["REG"]["outputs"], {"Q": {"width": 2, "signal": "y[1:0]"}})
        self.assertEqual(set(by_kind["REG"]["inputs"]), {"CLK", "ARST", "EN", "D"})
        self.assertEqual(by_kind["MEMWR"]["outputs"], {}, "a write port has no result pin")
        self.assertEqual(by_kind["MEMWR"]["width"], 2)
        self.assertEqual(by_kind["MEMWR"]["parameters"]["MEMID"], "\\u.mem")
        self.assertEqual(by_kind["MEMRD"]["outputs"], {"DATA": {"width": 2, "signal": "y[3:2]"}})
        self.assertEqual(r["memories"], [{"name": "u.mem", "width": 2, "size": 4, "start_offset": 0, "bits": 8,
                                          "read_ports": 1, "write_ports": 1, "src": "f.sv:7.1"}])
        self.assertEqual(r["summary"]["register_bits"], 2)
        self.assertEqual(r["summary"]["memory_bits"], 8)

    def test_ids_are_deterministic_and_independent_of_cell_order(self) -> None:
        cells = {
            "$b": cell("$eq", {"A": [2, 3], "B": [6, 7], "Y": [13]}, {"Y_WIDTH": "1"}),
            "$a": cell("$sub", {"A": [2, 3, 4, 5], "B": [6, 7, "0", "0"], "Y": [8, 9, 10, 11]}, {"Y_WIDTH": "100"}),
        }
        r1 = word_level.build_report(fake_module(cells), "top")
        r2 = word_level.build_report(fake_module(dict(reversed(list(cells.items())))), "top")
        self.assertEqual(r1, r2)
        self.assertEqual([(op["id"], op["kind"]) for op in r1["operations"]], [("op0000", "EQ"), ("op0001", "SUB")])

    def test_unsupported_cell_is_an_error_not_an_omission(self) -> None:
        data = fake_module({
            "$1": cell("$add", {"A": [2], "B": [3], "Y": [13]}, {"Y_WIDTH": "1"}),
            "$2": cell("$lut", {"A": [2, 3], "Y": [8]}, {"WIDTH": "10", "LUT": "0110"}),
            "$3": cell("$_AND_", {"A": [2], "B": [3], "Y": [9]}),
        })
        with self.assertRaises(boolean_graph.UnsupportedCell) as cm:
            word_level.build_report(data, "top")
        self.assertIn("$_AND_ x1", str(cm.exception))
        self.assertIn("$lut x1", str(cm.exception))

    def test_cli(self) -> None:
        data = fake_module({"$1": cell("$not", {"A": [2, 3, 4, 5], "Y": [8, 9, 10, 11]}, {"Y_WIDTH": "100"})})
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td) / "in.json", Path(td) / "word_level.json"
            src.write_text(json.dumps(data))
            proc = subprocess.run([sys.executable, str(TOOL / "word_level.py"), str(src), "--top", "top", "-o", str(out)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["by_kind"], {"NOT": 1})
            self.assertEqual(json.loads(out.read_text())["operations"][0]["outputs"]["Y"]["signal"], "y[3:0]")
            proc = subprocess.run([sys.executable, str(TOOL / "word_level.py"), str(src), "--top", "nope"],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("[word_level] error", proc.stderr)


@unittest.skipUnless(YOSYS, "yosys binary not found")
class WordLevelFlowTests(unittest.TestCase):
    """bool_area.py writes word.ys / word.log / word_yosys.json / word_level.json from a separate yosys run."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="word_level_test_")
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

    def test_adder8_is_one_add(self) -> None:
        out, m = self.flow("adder8", "adder8", CORPUS / "adder8.sv")
        for name in ("word.ys", "word.log", "word_yosys.json", "word_level.json"):
            self.assertTrue((out / name).exists(), name)
        r = json.loads((out / "word_level.json").read_text())
        self.assertEqual(r["summary"]["by_kind"], {"ADD": 1})
        op = r["operations"][0]
        self.assertEqual(op["width"], 9)
        self.assertEqual(op["inputs"]["A"]["signal"], "{1'b0, a}")
        self.assertEqual(op["inputs"]["B"]["signal"], "{1'b0, b}")
        self.assertEqual(op["outputs"]["Y"]["signal"], "{cout, sum}")
        self.assertEqual(op["signed"], {"A": False, "B": False})
        self.assertTrue(op["src"].startswith(f"{CORPUS / 'adder8.sv'}:3."), op["src"])
        self.assertEqual(m["word_level"], {"schema_version": word_level.WORD_SCHEMA_VERSION, **r["summary"]})
        self.assertIn("word_seconds", m["timing"])

    def test_sync_fifo_operations_memory_and_determinism(self) -> None:
        out, _ = self.flow("a", "sync_fifo", EXAMPLES / "sync_fifo.sv")
        r = json.loads((out / "word_level.json").read_text())
        kinds = r["summary"]["by_kind"]
        for k in ("ADD", "EQ", "MUX", "MEMRD", "MEMWR", "REG"):
            self.assertIn(k, kinds, kinds)
        self.assertEqual(r["memories"], [{
            "name": "mem", "width": 32, "size": 16, "start_offset": 0, "bits": 512, "read_ports": 1, "write_ports": 1,
            "src": r["memories"][0]["src"]}])
        self.assertTrue(r["memories"][0]["src"].startswith(f"{EXAMPLES / 'sync_fifo.sv'}:"), r["memories"][0]["src"])
        self.assertEqual(r["summary"]["register_bits"], 4 + 4 + 5)
        self.assertEqual(r["summary"]["memory_bits"], 16 * 32)
        for op in r["operations"]:
            for pin in (*op["inputs"].values(), *op["outputs"].values()):
                self.assertNotIn("$bit", pin["signal"], op)
            if op["src"]:
                self.assertTrue(Path(op["src"].rsplit(":", 1)[0]).is_absolute(), op["src"])
        regs = [op for op in r["operations"] if op["kind"] == "REG"]
        self.assertEqual(sorted(op["outputs"]["Q"]["signal"] for op in regs), ["count_o", "rd_ptr_q", "wr_ptr_q"])
        out2, _ = self.flow("b", "sync_fifo", EXAMPLES / "sync_fifo.sv")
        self.assertTrue(filecmp.cmp(out / "word_level.json", out2 / "word_level.json", shallow=False))

    def test_word_stage_failure_is_reported(self) -> None:
        bad = self.tmp / "bad.sv"
        bad.write_text("module bad(input logic a, output logic y);\n  assign y = a +;\nendmodule\n")
        out = self.tmp / "bad"
        proc = subprocess.run([sys.executable, str(TOOL / "bool_area.py"), str(bad), "--top", "bad", "-o", str(out), "-q"],
                              capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        m = json.loads((out / "metrics.json").read_text())
        self.assertEqual(m["status"], "failed")
        self.assertEqual(m["stage"], "parse")
        self.assertTrue(m["errors"], m)
        self.assertFalse((out / "word_level.json").exists())
        self.assertFalse((out / "generic_yosys.json").exists(), "synthesis must not run after the word stage failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
