#!/usr/bin/env python3
"""
Unit tests for boolean_graph.py on hand-built write_json documents (no yosys needed).

    python3 tools/synth_area/tests/test_boolean_graph.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
sys.path.insert(0, str(TOOL))
import boolean_graph


def fake_yosys_json(cells: dict, ports: dict, netnames: dict | None = None) -> dict:
    return {"modules": {"top": {"ports": ports, "cells": cells, "netnames": netnames or {}}}}

class GraphUnitTests(unittest.TestCase):
    """boolean_graph on hand-built write_json documents (no yosys needed)."""

    def test_and_not_chain(self) -> None:
        # y = ~(a & b): a[2], b[3] -> AND -> 4 -> NOT -> y[5]
        data = fake_yosys_json(
            cells={
                "g1": {"type": "$_AND_", "connections": {"A": [2], "B": [3], "Y": [4]},
                       "port_directions": {"A": "input", "B": "input", "Y": "output"}},
                "g2": {"type": "$_NOT_", "connections": {"A": [4], "Y": [5]},
                       "port_directions": {"A": "input", "Y": "output"}},
            },
            ports={"a": {"direction": "input", "bits": [2]}, "b": {"direction": "input", "bits": [3]},
                   "y": {"direction": "output", "bits": [5]}},
        )
        g = boolean_graph.build_graph(data, "top")
        m = boolean_graph.compute_metrics(g)
        self.assertEqual((m["and"], m["not"], m["gate_total"], m["dff"]), (1, 1, 2, 0))
        self.assertEqual(m["max_depth"], 2)
        self.assertEqual(m["node_total"], len(g["nodes"]))
        self.assertEqual(m["edge_total"], 4)  # a->AND, b->AND, AND->NOT, NOT->y
        self.assertEqual(sorted(e["pin"] for e in g["edges"]), ["A", "A", "A", "B"])
        self.assertEqual(g, boolean_graph.build_graph(json.loads(json.dumps(data)), "top"), "graph must be deterministic")

    def test_dff_is_depth_cut_and_clock_not_data_fanout(self) -> None:
        # q <= ~q ; depth of the NOT is 1, loop through the DFF is not a combinational loop
        data = fake_yosys_json(
            cells={
                "inv": {"type": "$_NOT_", "connections": {"A": [3], "Y": [4]},
                        "port_directions": {"A": "input", "Y": "output"}},
                "ff": {"type": "$_DFF_P_", "connections": {"C": [2], "D": [4], "Q": [3]},
                       "port_directions": {"C": "input", "D": "input", "Q": "output"}},
            },
            ports={"clk": {"direction": "input", "bits": [2]}, "q": {"direction": "output", "bits": [3]}},
        )
        g = boolean_graph.build_graph(data, "top", {"$_DFF_P_"})
        m = boolean_graph.compute_metrics(g)
        self.assertEqual((m["dff"], m["not"], m["max_depth"]), (1, 1, 1))
        self.assertFalse(m["combinational_loop"])
        self.assertEqual(m["clock_fanout"], {"clk": 1})
        self.assertEqual(m["max_fanout"], 2)  # q drives NOT and the output port; the clock edge is not counted
        self.assertEqual(m["dff_by_type"], {"$_DFF_P_": 1})

    def test_clock_and_reset_logic_not_counted_as_data_depth(self) -> None:
        # D fed straight from an input (depth 0); C through NOT->NOT->NOT; async R through one NOT
        data = fake_yosys_json(
            cells={
                "c1": {"type": "$_NOT_", "connections": {"A": [2], "Y": [10]}},
                "c2": {"type": "$_NOT_", "connections": {"A": [10], "Y": [11]}},
                "c3": {"type": "$_NOT_", "connections": {"A": [11], "Y": [12]}},
                "r1": {"type": "$_NOT_", "connections": {"A": [4], "Y": [13]}},
                "ff": {"type": "$_DFF_PN0_", "connections": {"C": [12], "D": [3], "R": [13], "Q": [5]}},
            },
            ports={"clk": {"direction": "input", "bits": [2]}, "d": {"direction": "input", "bits": [3]},
                   "rst_n": {"direction": "input", "bits": [4]}, "q": {"direction": "output", "bits": [5]}},
        )
        m = boolean_graph.compute_metrics(boolean_graph.build_graph(data, "top", {"$_DFF_PN0_"}))
        self.assertEqual(m["max_depth"], 0)
        self.assertEqual(m["gate_total"], 4)
        # the same gates on the D path do count
        data["modules"]["top"]["cells"]["ff"]["connections"] = {"C": [2], "D": [12], "R": [4], "Q": [5]}
        data["modules"]["top"]["cells"]["c1"]["connections"] = {"A": [3], "Y": [10]}
        m = boolean_graph.compute_metrics(boolean_graph.build_graph(data, "top", {"$_DFF_PN0_"}))
        self.assertEqual(m["max_depth"], 3)

    def test_reset_fanout_reported_separately_from_data_fanout(self) -> None:
        # one reset into three flops' R pins; the widest data fanout is d -> two D pins
        cells = {f"ff{i}": {"type": "$_DFF_PN0_", "connections": {"C": [2], "D": [3 if i < 2 else 6], "R": [4], "Q": [10 + i]}}
                 for i in range(3)}
        data = fake_yosys_json(
            cells=cells,
            ports={"clk": {"direction": "input", "bits": [2]}, "d": {"direction": "input", "bits": [3]},
                   "rst_n": {"direction": "input", "bits": [4]}, "e": {"direction": "input", "bits": [6]},
                   "q": {"direction": "output", "bits": [10]}},
        )
        m = boolean_graph.compute_metrics(boolean_graph.build_graph(data, "top", {"$_DFF_PN0_"}))
        self.assertEqual(m["max_fanout"], 2)
        self.assertEqual(m["clock_fanout"], {"clk": 3})
        self.assertEqual(m["control_fanout"], {"rst_n": 3})
        self.assertEqual(m["edge_total"], 10)

    def test_vector_bits_named_with_hdl_index(self) -> None:
        data = fake_yosys_json(
            cells={"b0": {"type": "$_NOT_", "connections": {"A": [2], "Y": [12]}},
                   "b1": {"type": "$_NOT_", "connections": {"A": [3], "Y": [13]}}},
            ports={"hi": {"direction": "input", "bits": [2, 3], "offset": 4},          # input [5:4] hi
                   "up": {"direction": "input", "bits": [6, 7], "upto": 1},            # input [0:1] up (unused)
                   "y": {"direction": "output", "bits": [12, 13], "offset": 3, "upto": 1}},  # output [3:4] y
            netnames={"t": {"bits": [12, 13], "offset": 3, "upto": 1, "hide_name": 0},
                      "one": {"bits": [3], "offset": 7, "hide_name": 0}},
        )
        g = boolean_graph.build_graph(data, "top", set())
        names = {(n["port"], n["bit"]): n["name"] for n in g["nodes"] if n["kind"] in ("INPUT", "OUTPUT")}
        self.assertEqual(names[("hi", 4)], "hi[4]")
        self.assertEqual(names[("hi", 5)], "hi[5]")
        self.assertEqual(names[("up", 1)], "up[1]")  # first list entry of an ascending range is the high index
        self.assertEqual(names[("up", 0)], "up[0]")
        self.assertEqual(names[("y", 4)], "y[4]")
        self.assertEqual(names[("y", 3)], "y[3]")
        self.assertEqual([n["name"] for n in g["nodes"] if n["kind"] == "GATE"], ["t[4]", "t[3]"])
        self.assertEqual(boolean_graph.bit_label("one", {"bits": [3], "offset": 7}, 0), "one[7]")
        self.assertEqual(boolean_graph.bit_label("s", {"bits": [3]}, 0), "s")

    def test_cli_enforces_dff_allowlist(self) -> None:
        data = fake_yosys_json(
            cells={"ff": {"type": "$_DFF_NN0_", "connections": {"C": [2], "D": [3], "R": [4], "Q": [5]}}},
            ports={"clk": {"direction": "input", "bits": [2]}, "d": {"direction": "input", "bits": [3]},
                   "r": {"direction": "input", "bits": [4]}, "q": {"direction": "output", "bits": [5]}},
        )
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.json"
            src.write_text(json.dumps(data))
            out = Path(td) / "g.json"
            common = [str(src), "--top", "top", "-o", str(out)]
            # default profile (asap7_rvt_tt_v1) lists no negative-clock reset flop -> rejected
            self.assertEqual(boolean_graph.main(common), 1)
            self.assertFalse(out.exists())
            self.assertEqual(boolean_graph.main([*common, "--dff-type", "$_DFF_NN0_"]), 0)
            self.assertTrue(out.exists())
            self.assertEqual(boolean_graph.main([*common, "--profile", "no_such_profile"]), 1)

    def test_unsupported_cell_rejected(self) -> None:
        data = fake_yosys_json(
            cells={"m": {"type": "$_DLATCH_P_", "connections": {"E": [2], "D": [3], "Q": [4]},
                         "port_directions": {"E": "input", "D": "input", "Q": "output"}}},
            ports={"e": {"direction": "input", "bits": [2]}, "d": {"direction": "input", "bits": [3]},
                   "q": {"direction": "output", "bits": [4]}},
        )
        with self.assertRaises(boolean_graph.UnsupportedCell):
            boolean_graph.build_graph(data, "top", {"$_DFF_P_"})
        # a word-level cell that should have been lowered is also rejected, not approximated
        data["modules"]["top"]["cells"] = {"a": {"type": "$add", "connections": {"A": [2], "B": [3], "Y": [4]},
                                                 "port_directions": {"A": "input", "B": "input", "Y": "output"}}}
        with self.assertRaises(boolean_graph.UnsupportedCell):
            boolean_graph.build_graph(data, "top", {"$_DFF_P_"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
