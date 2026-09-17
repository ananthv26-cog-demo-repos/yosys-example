#!/usr/bin/env python3
"""
Unit tests for diff_sim.py port classification (no tools needed).

    python3 tools/synth_area/tests/test_diff_sim.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
sys.path.insert(0, str(TOOL))
import diff_sim


class DiffSimUnitTests(unittest.TestCase):
    def test_classify_ports_infers_clocks_and_resets(self) -> None:
        ports = {
            "clk_i": {"direction": "input", "width": 1}, "rst_ni": {"direction": "input", "width": 1},
            "wclk_i": {"direction": "input", "width": 1}, "clk_en": {"direction": "input", "width": 1},
            "soft_rst": {"direction": "input", "width": 1}, "d_i": {"direction": "input", "width": 8},
            "q_o": {"direction": "output", "width": 8},
        }
        clocks, resets, ins, outs = diff_sim.classify_ports(ports)
        self.assertEqual(clocks, ["clk_i", "wclk_i"])
        self.assertEqual(resets, {"rst_ni": True, "soft_rst": False})
        self.assertEqual(ins, ["clk_en", "d_i"])
        self.assertEqual(outs, ["q_o"])

    def test_reset_syntax_wins_over_clock_syntax(self) -> None:
        ports = {"clk": {"direction": "input", "width": 1}, "clk_reset_n": {"direction": "input", "width": 1},
                 "q": {"direction": "output", "width": 1}}
        clocks, resets, ins, _ = diff_sim.classify_ports(ports)
        self.assertEqual((clocks, resets, ins), (["clk"], {"clk_reset_n": True}, []))
        tb = diff_sim.gen_testbench("top", ports, 10, 1)
        self.assertNotIn("clk_reset_n = ~clk_reset_n", tb)
        self.assertIn("clk_reset_n <= (cycle <= 4", tb)

    def test_parse_result_statuses(self) -> None:
        line = "DIFFSIM cycles=20 compared_bits={c} mismatches={m} gate_x_bits={x}"
        self.assertEqual(diff_sim.parse_result(line.format(c=40, m=0, x=0))["status"], "match")
        r = diff_sim.parse_result(line.format(c=40, m=0, x=5))
        self.assertEqual((r["status"], r["gate_x_bits"]), ("match", 5))
        self.assertEqual(diff_sim.parse_result(line.format(c=40, m=0, x=40))["status"], "failed")
        self.assertEqual(diff_sim.parse_result(line.format(c=0, m=0, x=0))["status"], "failed")
        self.assertEqual(diff_sim.parse_result("MISMATCH cycle=3 q[0] rtl=1 gate=0\n" + line.format(c=40, m=1, x=0)),
                         {"cycles": 20, "compared_bits": 40, "mismatches": 1, "gate_x_bits": 0, "status": "mismatch",
                          "first_mismatches": ["MISMATCH cycle=3 q[0] rtl=1 gate=0"]})
        self.assertEqual(diff_sim.parse_result("")["status"], "failed")

    def test_classify_ports_explicit_overrides(self) -> None:
        ports = {"c": {"direction": "input", "width": 1}, "r": {"direction": "input", "width": 1},
                 "d": {"direction": "input", "width": 1}, "q": {"direction": "output", "width": 1}}
        clocks, resets, ins, _ = diff_sim.classify_ports(ports, clocks=["c"], resets={"r": True})
        self.assertEqual((clocks, resets, ins), (["c"], {"r": True}, ["d"]))
        with self.assertRaises(ValueError):
            diff_sim.classify_ports(ports, clocks=["nope"])

    def test_classify_ports_partial_override_keeps_inferring_the_other(self) -> None:
        ports = {"clk": {"direction": "input", "width": 1}, "rst_n": {"direction": "input", "width": 1},
                 "d": {"direction": "input", "width": 1}, "q": {"direction": "output", "width": 1}}
        clocks, resets, ins, _ = diff_sim.classify_ports(ports, clocks=["clk"])
        self.assertEqual((clocks, resets, ins), (["clk"], {"rst_n": True}, ["d"]))
        clocks, resets, ins, _ = diff_sim.classify_ports(ports, resets={"rst_n": True})
        self.assertEqual((clocks, resets, ins), (["clk"], {"rst_n": True}, ["d"]))

    def test_reset_is_a_name_token_not_a_substring(self) -> None:
        for name in ("rst", "rst_ni", "reset", "reset_n", "arst_n", "wrst_ni", "hresetn", "soft_rst", "RstN", "nrst",
                     "sync_reset_i"):
            self.assertTrue(diff_sim.is_reset_name(name), name)
        for name in ("burst_i", "first", "thirst_o", "resetting_count", "d", "presto"):
            self.assertFalse(diff_sim.is_reset_name(name), name)
        ports = {"clk": {"direction": "input", "width": 1}, "burst_i": {"direction": "input", "width": 1},
                 "q": {"direction": "output", "width": 1}}
        clocks, resets, ins, _ = diff_sim.classify_ports(ports)
        self.assertEqual((clocks, resets, ins), (["clk"], {}, ["burst_i"]))

    def test_testbench_drives_inputs_nonblocking_and_holds_reset_for_reset_cycles(self) -> None:
        ports = {"clk": {"direction": "input", "width": 1}, "clk2": {"direction": "input", "width": 1},
                 "rst_n": {"direction": "input", "width": 1}, "d": {"direction": "input", "width": 8},
                 "q": {"direction": "output", "width": 8}}
        tb = diff_sim.gen_testbench("top", ports, 10, 1, reset_cycles=4)
        # a secondary clock edge that coincides with the primary falling edge must not race the stimulus
        self.assertIn("    d <= $random(seed)", tb)
        self.assertIn("    rst_n <= (cycle <= 4 ||", tb)
        self.assertNotIn("    d = $random", tb)
        self.assertNotIn("    rst_n = (", tb)

    def test_nonpositive_cycles_is_a_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="diff_sim_test_") as td:
            res = diff_sim.run_diff_sim(top="t", ports={}, rtl_sources=[], includes=[], defines=[], mapped_json=Path(td),
                                        libs=[], yosys="yosys", sv2v=None, iverilog="iverilog", vvp="vvp",
                                        work=Path(td) / "w", cycles=0, seed=1, timeout=5)
        self.assertEqual(res["status"], "failed")
        self.assertIn("at least 1", res["error"])

    def test_missing_tool_is_a_structured_failure(self) -> None:
        ports = {"clk": {"direction": "input", "width": 1}, "d": {"direction": "input", "width": 1},
                 "q": {"direction": "output", "width": 1}}
        with tempfile.TemporaryDirectory(prefix="diff_sim_test_") as td:
            missing = str(Path(td) / "no_such_yosys")
            res = diff_sim.run_diff_sim(top="t", ports=ports, rtl_sources=[], includes=[], defines=[],
                                        mapped_json=Path(td) / "m.json", libs=[], yosys=missing, sv2v=None,
                                        iverilog="iverilog", vvp="vvp", work=Path(td) / "w", cycles=5, seed=1, timeout=5)
        self.assertEqual(res["status"], "failed")
        self.assertIn("cannot run", res["error"])
        self.assertIn("no_such_yosys", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
