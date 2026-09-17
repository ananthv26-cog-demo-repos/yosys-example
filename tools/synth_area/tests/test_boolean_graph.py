#!/usr/bin/env python3
"""
Unit tests for boolean_graph.py on hand-built write_json documents (no yosys needed).

    python3 tools/synth_area/tests/test_boolean_graph.py
"""

from __future__ import annotations

import json
import sys
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
