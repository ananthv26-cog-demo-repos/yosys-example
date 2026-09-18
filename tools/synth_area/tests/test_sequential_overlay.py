#!/usr/bin/env python3
"""
Tests for sequential_overlay.py: unit tests on hand-built write_json documents (no yosys needed)
and end-to-end checks of the sequential_overlay.json that bool_area.py writes.

    python3 tools/synth_area/tests/test_sequential_overlay.py
"""

from __future__ import annotations

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
import sequential_overlay
import word_level

# clk=2 rst=3 en=4 d[3:0]=5..8 q[3:0]=9..12 (init 1010) t=13 (init 1) ld=14 ad[3:0]=15..18 s=19 c=20
NETNAMES = {
    "clk": {"bits": [2], "hide_name": 0},
    "rst": {"bits": [3], "hide_name": 0},
    "en": {"bits": [4], "hide_name": 0},
    "d": {"bits": [5, 6, 7, 8], "hide_name": 0},
    "q": {"bits": [9, 10, 11, 12], "hide_name": 0, "attributes": {"init": "1010"}},
    "t": {"bits": [13], "hide_name": 0, "attributes": {"init": "1"}},
    "ld": {"bits": [14], "hide_name": 0},
    "ad": {"bits": [15, 16, 17, 18], "hide_name": 0},
    "s": {"bits": [19], "hide_name": 0},
    "c": {"bits": [20], "hide_name": 0},
}
Q, D, AD = [9, 10, 11, 12], [5, 6, 7, 8], [15, 16, 17, 18]


def doc(cells: dict, netnames: dict | None = None) -> dict:
    return {"modules": {"top": {"ports": {}, "cells": cells, "netnames": NETNAMES if netnames is None else netnames}}}


def reg(ctype: str, conns: dict, params: dict, src: str | None = None) -> dict:
    c = {"type": ctype, "connections": conns, "parameters": params}
    if src:
        c["attributes"] = {"src": src}
    return c


class HelperTests(unittest.TestCase):
    def test_const_text(self) -> None:
        self.assertEqual(sequential_overlay.const_text(3, 4), "4'b0011")
        self.assertEqual(sequential_overlay.const_text(0, 1), "1'b0")
        self.assertEqual(sequential_overlay.const_text("x1", 2), "2'bx1")

    def test_bit_label_matches_boolean_graph_convention(self) -> None:
        names = word_level.bit_names({"netnames": NETNAMES})
        self.assertEqual(sequential_overlay.bit_label(13, names), "t")
        self.assertEqual(sequential_overlay.bit_label(9, names), "q[0]")
        self.assertEqual(sequential_overlay.bit_label(12, names), "q[3]")
        self.assertEqual(sequential_overlay.bit_label("1", names), "1'b1")
        self.assertEqual(sequential_overlay.bit_label(99, names), "$bit99")

    def test_alias_choice_joins_boolean_graph_dff_names(self) -> None:
        """Lexical order (`aa` < `z`) and length order (`z` shorter) disagree: both layers must still pick
        the same alias, or the overlay's per-bit `q` cannot be joined to the Boolean graph's DFF node."""
        netnames = {
            "clk": {"bits": [2], "hide_name": 0}, "d": {"bits": [5], "hide_name": 0},
            "aa": {"bits": [9], "hide_name": 0}, "z": {"bits": [9], "hide_name": 0},
            "$hidden": {"bits": [9], "hide_name": 1},
        }
        overlay = sequential_overlay.build_overlay(doc({
            "r": reg("$dff", {"CLK": [2], "D": [5], "Q": [9]}, {"WIDTH": 1, "CLK_POLARITY": 1}),
        }, netnames), "top")
        graph = boolean_graph.build_graph({"modules": {"top": {"ports": {}, "netnames": netnames, "cells": {
            "r": {"type": "$_DFF_P_", "connections": {"C": [2], "D": [5], "Q": [9]}},
        }}}}, "top")
        dff_names = [n["name"] for n in graph["nodes"] if n["kind"] == "DFF"]
        self.assertEqual(dff_names, [overlay["registers"][0]["bits"][0]["q"]])

    def test_init_bits_are_msb_first_and_skip_x(self) -> None:
        inits = sequential_overlay.init_bits({"netnames": {
            **NETNAMES,
            "w": {"bits": [30, 31, 32], "attributes": {"init": "x01"}},
            "n": {"bits": [40, 41], "attributes": {"init": 2}},
        }})
        self.assertEqual({b: inits[b] for b in (9, 10, 11, 12)}, {9: "0", 10: "1", 11: "0", 12: "1"})
        self.assertEqual(inits[13], "1")
        self.assertEqual((inits[30], inits[31]), ("1", "0"))
        self.assertNotIn(32, inits)
        self.assertEqual((inits[40], inits[41]), ("0", "1"), "integer init values are decoded too")


class OverlayTests(unittest.TestCase):
    def build(self, cells: dict, **kw) -> dict:
        return sequential_overlay.build_overlay(doc(cells), "top", **kw)

    def test_plain_dff_and_negedge_clock(self) -> None:
        r = self.build({"$t": reg("$dff", {"CLK": [2], "D": [13], "Q": [13]}, {"CLK_POLARITY": "0", "WIDTH": "1"})})
        (t,) = r["registers"]
        self.assertEqual(t["id"], "reg0000")
        self.assertEqual(t["clock"], {"signal": "clk", "edge": "negedge"})
        self.assertIsNone(t["reset"])
        self.assertIsNone(t["enable"])
        self.assertEqual(t["init"], "1'b1")
        self.assertEqual(t["bits"], [{"q": "t", "d": "t", "init": "1"}])
        self.assertEqual(r["summary"]["clocks"], [{"signal": "clk", "edge": "negedge", "registers": 1, "bits": 1}])
        self.assertEqual(r["summary"]["reset_bits"], {"none": 1})

    def test_async_reset_with_enable_value_polarity_and_init(self) -> None:
        r = self.build({"$q": reg(
            "$adffe", {"CLK": [2], "ARST": [3], "EN": [4], "D": D, "Q": Q},
            {"CLK_POLARITY": "1", "ARST_POLARITY": "0", "ARST_VALUE": "0011", "EN_POLARITY": "1", "WIDTH": "100"},
            src="a.sv:3.5",
        )}, src_base=Path("/base"))
        (q,) = r["registers"]
        self.assertEqual(q["width"], 4)
        self.assertEqual(q["reset"], {"kind": "async", "signal": "rst", "active": "low", "value": "4'b0011"})
        self.assertEqual(q["enable"], {"signal": "en", "active": "high"})
        self.assertEqual(q["init"], "4'b1010")
        self.assertEqual([b["q"] for b in q["bits"]], ["q[0]", "q[1]", "q[2]", "q[3]"])
        self.assertEqual([b["d"] for b in q["bits"]], ["d[0]", "d[1]", "d[2]", "d[3]"])
        self.assertEqual([b["init"] for b in q["bits"]], ["0", "1", "0", "1"])
        self.assertEqual(q["src"], "/base/a.sv:3.5")
        s = r["summary"]
        self.assertEqual(s["resets"], [{"signal": "rst", "kind": "async", "active": "low", "registers": 1, "bits": 4}])
        self.assertEqual((s["enable_bits"], s["init_bits"], s["reset_bits"]), (4, 4, {"async": 4}))

    def test_sync_reset_priority_over_enable(self) -> None:
        base = {"CLK": [2], "SRST": [3], "EN": [4], "D": D, "Q": Q}
        params = {"CLK_POLARITY": "1", "SRST_POLARITY": "1", "SRST_VALUE": "0000", "EN_POLARITY": "0", "WIDTH": "100"}
        r = self.build({"$a": reg("$sdffe", base, params), "$b": reg("$sdffce", dict(base, Q=AD), params)})
        by_type = {x["yosys_type"]: x for x in r["registers"]}
        self.assertEqual(by_type["$sdffe"]["reset"], {"kind": "sync", "signal": "rst", "active": "high",
                                                      "value": "4'b0000", "gated_by_enable": False})
        self.assertTrue(by_type["$sdffce"]["reset"]["gated_by_enable"])
        self.assertEqual(by_type["$sdffe"]["enable"], {"signal": "en", "active": "low"})
        self.assertEqual(r["summary"]["resets"][0]["bits"], 8)

    def test_async_load_and_set_clear(self) -> None:
        r = self.build({
            "$l": reg("$aldff", {"CLK": [2], "ALOAD": [14], "AD": AD, "D": D, "Q": Q},
                      {"CLK_POLARITY": "1", "ALOAD_POLARITY": "1", "WIDTH": "100"}),
            "$s": reg("$dffsr", {"CLK": [2], "SET": [19], "CLR": [20], "D": [13], "Q": [13]},
                      {"CLK_POLARITY": "1", "SET_POLARITY": "1", "CLR_POLARITY": "0", "WIDTH": "1"}),
        })
        by_type = {x["yosys_type"]: x for x in r["registers"]}
        self.assertEqual(by_type["$aldff"]["reset"], {"kind": "async_load", "signal": "ld", "active": "high",
                                                      "value": "ad"})
        self.assertEqual(by_type["$dffsr"]["reset"], {"kind": "async_set_clear",
                                                      "set": {"signal": "s", "active": "high"},
                                                      "clear": {"signal": "c", "active": "low"}})
        self.assertEqual(r["summary"]["reset_bits"], {"async_load": 4, "async_set_clear": 1})
        self.assertEqual(r["summary"]["resets"], [
            {"signal": "c", "kind": "async_clear", "active": "low", "registers": 1, "bits": 1},
            {"signal": "ld", "kind": "async_load", "active": "high", "registers": 1, "bits": 4},
            {"signal": "s", "kind": "async_set", "active": "high", "registers": 1, "bits": 1},
        ], "set and clear nets are grouped as their own reset controls")

    def test_unknown_state_holding_cell_fails_loudly(self) -> None:
        with self.assertRaises(sequential_overlay.UnsupportedCell) as cm:
            self.build({
                "$r": reg("$dff", {"CLK": [2], "D": [13], "Q": [13]}, {"CLK_POLARITY": 1, "WIDTH": 1}),
                "$l": reg("$dlatch", {"EN": [4], "D": D, "Q": Q}, {"EN_POLARITY": 1, "WIDTH": 4}),
            })
        self.assertIn("$dlatch x1", str(cm.exception))

    def test_constant_d_bits_and_missing_names(self) -> None:
        r = self.build({"$q": reg("$dff", {"CLK": [2], "D": ["0", 77, 7, 8], "Q": Q},
                                  {"CLK_POLARITY": "1", "WIDTH": "100"})})
        self.assertEqual([b["d"] for b in r["registers"][0]["bits"]], ["1'b0", "$bit77", "d[2]", "d[3]"])
        self.assertEqual(r["registers"][0]["d"], "{d[3:2], $bit77, 1'b0}")

    def test_non_register_cells_are_skipped_and_order_is_deterministic(self) -> None:
        cells = {
            "$add": {"type": "$add", "connections": {"A": D, "B": D, "Y": AD}, "parameters": {}},
            "$z": reg("$dff", {"CLK": [2], "D": [13], "Q": [13]}, {"CLK_POLARITY": "1", "WIDTH": "1"}),
            "$a": reg("$dff", {"CLK": [2], "D": D, "Q": Q}, {"CLK_POLARITY": "1", "WIDTH": "100"}),
        }
        r1 = self.build(cells)
        r2 = self.build(dict(reversed(list(cells.items()))))
        self.assertEqual(r1, r2)
        self.assertEqual([(x["id"], x["q"]) for x in r1["registers"]], [("reg0000", "q"), ("reg0001", "t")])
        self.assertEqual(r1["summary"]["by_type"], {"$dff": 2})

    def test_width_mismatch_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            self.build({"$q": reg("$dff", {"CLK": [2], "D": [5, 6], "Q": Q}, {"CLK_POLARITY": "1", "WIDTH": "100"})})

    def test_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "w.json"
            src.write_text(json.dumps(doc({"$t": reg("$dff", {"CLK": [2], "D": [13], "Q": [13]},
                                                     {"CLK_POLARITY": "1", "WIDTH": "1"})})))
            out = Path(td) / "seq.json"
            proc = subprocess.run([sys.executable, str(TOOL / "sequential_overlay.py"), str(src), "--top", "top",
                                   "-o", str(out)], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(out.read_text())["summary"]["register_bits"], 1)
            self.assertIn('"register_bits": 1', proc.stdout)
            bad = subprocess.run([sys.executable, str(TOOL / "sequential_overlay.py"), str(src), "--top", "nope",
                                  "-o", str(out)], capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("[sequential_overlay] error", bad.stderr)


class OverlayFlowTests(unittest.TestCase):
    """bool_area.py writes sequential_overlay.json next to word_level.json; registers match the RTL."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="seq_overlay_test_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def flow(self, name: str, top: str, src: Path) -> tuple[dict, dict]:
        out = self.tmp / name
        proc = subprocess.run(
            [sys.executable, str(TOOL / "bool_area.py"), str(src), "--top", top, "-o", str(out), "-q", "--no-equiv",
             "--sim-cycles", "0"], capture_output=True, text=True, check=False,
        )
        metrics = json.loads((out / "metrics.json").read_text())
        self.assertEqual(proc.returncode, 0, metrics.get("errors", proc.stderr))
        return json.loads((out / "sequential_overlay.json").read_text()), metrics

    def test_async_low_reset_flop(self) -> None:
        r, m = self.flow("dff", "dff_async_rst", CORPUS / "dff_async_rst.sv")
        (q,) = r["registers"]
        self.assertEqual((q["q"], q["d"], q["width"]), ("q", "d", 1))
        self.assertEqual(q["clock"], {"signal": "clk", "edge": "posedge"})
        self.assertEqual(q["reset"], {"kind": "async", "signal": "rst_n", "active": "low", "value": "1'b0"})
        self.assertIsNone(q["enable"])
        self.assertTrue(q["src"].startswith(f"{CORPUS / 'dff_async_rst.sv'}:3."), q["src"])
        self.assertEqual(m["sequential"]["register_bits"], m["boolean"]["dff"])

    def test_counter_with_sync_reset_and_enable(self) -> None:
        r, m = self.flow("cnt", "counter8_en", CORPUS / "counter8_en.sv")
        (c,) = r["registers"]
        self.assertEqual((c["q"], c["width"]), ("count", 8))
        self.assertEqual(c["reset"]["kind"], "sync")
        self.assertEqual((c["reset"]["signal"], c["reset"]["active"], c["reset"]["value"]), ("rst", "high", "8'b00000000"))
        self.assertIsNotNone(c["enable"])
        self.assertEqual([b["q"] for b in c["bits"]], [f"count[{i}]" for i in range(8)])
        graph = json.loads((self.tmp / "cnt" / "boolean_graph.json").read_text())
        dff_names = {n["name"] for n in graph["nodes"] if n["kind"] == "DFF"}
        self.assertEqual(dff_names, {b["q"] for b in c["bits"]}, "per-bit q labels join to Boolean-graph DFF nodes")
        self.assertEqual(m["sequential"]["enable_bits"], 8)

    def test_init_values_and_negedge_clock(self) -> None:
        src = self.tmp / "init_reg.sv"
        src.write_text(
            "module init_reg (input logic clk, rst_n, en, input logic [3:0] d, output logic [3:0] q,"
            " output logic t);\n"
            "    logic [3:0] q_r = 4'b1010;\n"
            "    always_ff @(posedge clk or negedge rst_n) if (!rst_n) q_r <= 4'd3; else if (en) q_r <= d;\n"
            "    always_ff @(negedge clk) t <= ~t;\n"
            "    assign q = q_r;\n"
            "endmodule\n"
        )
        r, _ = self.flow("init", "init_reg", src)
        by_q = {x["q"]: x for x in r["registers"]}
        self.assertEqual(by_q["q"]["init"], "4'b1010")
        self.assertEqual(by_q["q"]["reset"]["value"], "4'b0011")
        self.assertEqual(by_q["q"]["enable"], {"signal": "en", "active": "high"})
        self.assertEqual(by_q["t"]["clock"], {"signal": "clk", "edge": "negedge"})
        self.assertEqual(r["summary"]["clocks"], [
            {"signal": "clk", "edge": "negedge", "registers": 1, "bits": 1},
            {"signal": "clk", "edge": "posedge", "registers": 1, "bits": 4},
        ])
        self.assertEqual(r["summary"]["init_bits"], 4)

    def test_sync_fifo_registers(self) -> None:
        r, m = self.flow("fifo", "sync_fifo", EXAMPLES / "sync_fifo.sv")
        self.assertEqual(r["summary"]["register_bits"], 13)
        self.assertEqual({x["q"] for x in r["registers"]}, {"wr_ptr_q", "rd_ptr_q", "count_o"})
        self.assertTrue(all(x["reset"] and x["reset"]["kind"] == "async" for x in r["registers"]))
        self.assertEqual(m["sequential"]["register_bits"] + m["word_level"]["memory_bits"], m["boolean"]["dff"])


if __name__ == "__main__":
    unittest.main()
