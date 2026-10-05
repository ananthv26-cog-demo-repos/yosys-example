#!/usr/bin/env python3
"""
Smoke tests for the synth_area runner. Synthesis and Slurm-wrapper tests need a built yosys
(./build/yosys, $YOSYS or on PATH) and are skipped without one; InvocationTests always run;
sv2v tests are skipped when sv2v is not installed.

    python3 tools/synth_area/tests/test_synth_area.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
RUNNER = TOOL / "synth_area.py"
EXAMPLES = TOOL / "examples"

sys.path.insert(0, str(TOOL))
import synth_area

YOSYS = synth_area.find_yosys(None)
SV2V = synth_area.find_sv2v(None)


def run_runner(tmp: Path, top: str, sources: list[Path], *extra: str) -> tuple[int, dict]:
    out = tmp / f"{top}.json"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--top", top, "-o", str(out), "-q", *extra, *map(str, sources)],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode, json.loads(out.read_text())


@unittest.skipUnless(YOSYS, "yosys binary not found")
class SynthAreaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="synth_area_test_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_sync_fifo_slang(self) -> None:
        code, rep = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang")
        self.assertEqual(code, 0, rep["errors"])
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["frontend_used"], "slang")
        # 16 x 32 memory bits + 4+4 pointers + 5-bit count
        self.assertEqual(rep["stats"]["num_flops"], 16 * 32 + 4 + 4 + 5)
        self.assertGreater(rep["stats"]["num_comb_cells"], 0)
        self.assertIn("estimated_transistors", rep["stats"])

    def test_sync_fifo_verilog_frontend(self) -> None:
        code, rep = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "verilog")
        self.assertEqual(code, 0, rep["errors"])
        self.assertEqual(rep["frontend_used"], "verilog")

    @unittest.skipUnless(SV2V, "sv2v not found")
    def test_struct_fifo_sv2v(self) -> None:
        code, rep = run_runner(
            self.tmp, "struct_fifo", [EXAMPLES / "fifo_pkg.sv", EXAMPLES / "struct_fifo.sv"], "--frontend", "sv2v"
        )
        self.assertEqual(code, 0, rep["errors"])
        self.assertEqual(rep["frontend_used"], "sv2v")
        self.assertEqual([s["name"] for s in rep["stages"]], ["sv2v", "yosys[sv2v]"])

    def test_auto_prefers_slang(self) -> None:
        code, rep = run_runner(self.tmp, "if_fifo_top", [EXAMPLES / "if_fifo.sv"])
        self.assertEqual(code, 0, rep["errors"])
        self.assertEqual(rep["frontends_tried"], ["slang"])

    def test_auto_fallback_success_has_no_errors(self) -> None:
        # force the slang attempt to fail so auto falls through to sv2v
        code, rep = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--slang-arg=--no-such-option")
        self.assertEqual(code, 0, rep["errors"])
        self.assertEqual(rep["frontend_used"], "sv2v")
        self.assertEqual(rep["errors"], [])
        self.assertTrue(any("earlier attempt failed" in w for w in rep["warnings"]))
        self.assertEqual(sum("slang-only options" in w for w in rep["warnings"]), 1)

    def test_netlist_and_workdir(self) -> None:
        work = self.tmp / "scratch"
        code, rep = run_runner(
            self.tmp, "async_fifo", [EXAMPLES / "async_fifo.sv"], "--netlist", "--work-dir", str(work)
        )
        self.assertEqual(code, 0, rep["errors"])
        self.assertTrue(Path(rep["netlist"]).exists())
        self.assertTrue(Path(rep["log"]).exists())
        self.assertEqual(Path(rep["work_dir"]), work)

    def test_liberty_area(self) -> None:
        lib = TOOL.parent.parent / "examples" / "cmos" / "cmos_cells.lib"
        code, rep = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--liberty", str(lib))
        self.assertEqual(code, 0, rep["errors"])
        self.assertGreater(rep["stats"]["area"], 0)
        self.assertTrue(all(not t.startswith("$") for t in rep["stats"]["cells_by_type"]), "unmapped cells left")

    def test_failure_is_reported_not_raised(self) -> None:
        bad = self.tmp / "bad.sv"
        bad.write_text("module bad (input logic a, output logic y);\n  assign y = a +;\nendmodule\n")
        code, rep = run_runner(self.tmp, "bad", [bad], "--frontend", "slang")
        self.assertEqual(code, 1)
        self.assertEqual(rep["status"], "failed")
        self.assertTrue(rep["errors"])
        self.assertIn("bad.sv", rep["errors"][0])

    def test_relative_tool_paths_resolve_against_the_callers_cwd(self) -> None:
        # stages run in the work dir, so a relative --yosys must be pinned before that
        rel = os.path.relpath(YOSYS, self.tmp)
        self.assertFalse(os.path.isabs(rel))
        out = self.tmp / "rel.json"
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--top", "sync_fifo", "-o", str(out), "-q", "--yosys", rel,
             "--work-dir", str(self.tmp / "elsewhere"), "--frontend", "slang", str(EXAMPLES / "sync_fifo.sv")],
            capture_output=True, text=True, check=False, cwd=self.tmp,
        )
        rep = json.loads(out.read_text())
        self.assertEqual(proc.returncode, 0, rep["errors"])
        self.assertEqual(rep["yosys"], str(Path(YOSYS).resolve()))

    def test_compare_rejects_failed_and_zero_baselines(self) -> None:
        _, ok = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang")
        _, failed = run_runner(self.tmp, "x", [self.tmp / "nope.sv"])
        zero = {**ok, "stats": {**ok["stats"], "estimated_transistors": 0}}
        for name, rep in (("ok", ok), ("failed", failed), ("zero", zero)):
            (self.tmp / f"{name}.json").write_text(json.dumps(rep))
        for base, why in (("failed", "baseline failed"), ("zero", "baseline transistors is zero")):
            proc = subprocess.run(
                [sys.executable, str(TOOL / "compare_reports.py"), "--json", str(self.tmp / f"{base}.json"),
                 str(self.tmp / "ok.json")],
                capture_output=True, text=True, check=True,
            )
            rows = json.loads(proc.stdout)
            self.assertIsNone(rows[1]["delta_pct_vs_baseline"])
            self.assertIn(why, rows[1]["not_comparable"])

    def test_unknown_area_ids_match_stat_json_keys(self) -> None:
        txt = self.tmp / "stat.txt"
        txt.write_text(
            "   Area for cell type \\sram_macro is unknown!\n"
            "   Area for cell type \\macro cell is unknown!\n"
            "   Area for cell type \\$paramod\\x is unknown!\n"
            "   Area for cell type \\1bad is unknown!\n"
            "   Area for cell type $_DFF_P_ is unknown!\n"
        )
        self.assertEqual(
            synth_area.unknown_area_cells(txt),
            {"sram_macro", "macro cell", "\\$paramod\\x", "\\1bad", "$_DFF_P_"},
        )

    def test_blackbox_area_is_lower_bound(self) -> None:
        src = self.tmp / "bb.sv"
        src.write_text(
            "module sram_macro (input logic clk, input logic [3:0] a, input logic [7:0] d, output logic [7:0] q);\n"
            "endmodule\n"
            "module bb_top (input logic clk, input logic [3:0] a, input logic [7:0] d, output logic [7:0] q,\n"
            "               output logic [7:0] q2);\n"
            "  logic [7:0] r;\n"
            "  always_ff @(posedge clk) r <= d ^ {a, a};\n"
            "  sram_macro u (.clk, .a, .d(r), .q);\n"
            "  assign q2 = r + 8'd1;\n"
            "endmodule\n"
        )
        lib = TOOL.parent.parent / "examples" / "cmos" / "cmos_cells.lib"
        code, rep = run_runner(self.tmp, "bb_top", [src], "--frontend", "slang", "--empty-blackboxes",
                               "--liberty", str(lib))
        self.assertEqual(code, 0, rep["errors"])
        self.assertTrue(rep["stats"]["area_is_lower_bound"])
        self.assertEqual(rep["stats"]["unknown_area_cell_types"], {"sram_macro": 1})
        # a liberty cell declared without `area` is just as invisible to `stat -liberty`
        lib2 = self.tmp / "with_macro.lib"
        lib2.write_text(lib.read_text() + "\nlibrary(x) { cell(sram_macro) { pin(clk) { direction: input; } } }\n")
        _, rep2 = run_runner(self.tmp, "bb_top", [src], "--frontend", "slang", "--empty-blackboxes",
                             "--liberty", str(lib2))
        self.assertTrue(rep2["stats"]["area_is_lower_bound"])
        self.assertEqual(rep2["stats"]["unknown_area_cell_types"], {"sram_macro": 1})
        # a library with no `area` at all disables stat's area bookkeeping entirely: every cell is unknown
        lib3 = self.tmp / "no_area.lib"
        lib3.write_text(re.sub(r"^\s*area\s*:.*\n", "", lib.read_text(), flags=re.MULTILINE))
        _, rep3 = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang",
                             "--liberty", str(lib3))
        self.assertEqual(rep3["stats"]["area"], 0.0)
        self.assertTrue(rep3["stats"]["area_is_lower_bound"])
        self.assertEqual(rep3["stats"]["unknown_area_cell_types"], rep3["stats"]["cells_by_type"])
        # ...but a library whose cells legitimately have area 0 is complete, just zero
        # (plus a pinless filler cell: it has an area but no members for `select -count`)
        lib4 = self.tmp / "zero_area.lib"
        zero = re.sub(r"^(\s*area\s*:).*$", r"\1 0;", lib.read_text(), flags=re.MULTILINE)
        lib4.write_text(zero.replace("cell(BUF)", "cell(FILL) { area : 1; }\n  cell(BUF)", 1))
        self.assertIn("cell(FILL)", lib4.read_text())
        _, rep4 = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang",
                             "--liberty", str(lib4))
        self.assertEqual(rep4["stats"]["area"], 0.0)
        self.assertFalse(rep4["stats"]["area_is_lower_bound"])
        self.assertEqual(rep4["stats"]["unknown_area_cell_types"], {})
        # partial area must not silently be ranked against a complete one
        _, full = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang",
                             "--liberty", str(lib))
        (self.tmp / "full.json").write_text(json.dumps(full))
        (self.tmp / "bb.json").write_text(json.dumps(rep))
        proc = subprocess.run(
            [sys.executable, str(TOOL / "compare_reports.py"), "--json", str(self.tmp / "full.json"),
             str(self.tmp / "bb.json")],
            capture_output=True, text=True, check=True,
        )
        rows = json.loads(proc.stdout)
        self.assertIsNone(rows[1]["delta_pct_vs_baseline"])
        self.assertIn("partial", rows[1]["not_comparable"])

    def test_compare_reports(self) -> None:
        _, a = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang")
        (self.tmp / "a.json").write_text(json.dumps(a))
        proc = subprocess.run(
            [sys.executable, str(TOOL / "compare_reports.py"), "--json", str(self.tmp / "a.json"), str(self.tmp / "a.json")],
            capture_output=True, text=True, check=True,
        )
        rows = json.loads(proc.stdout)
        self.assertEqual(rows[1]["delta_pct_vs_baseline"], 0.0)

    def test_compare_rejects_mixed_metric_and_frontend(self) -> None:
        _, a = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang")
        _, b = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "verilog")
        lib = TOOL.parent.parent / "examples" / "cmos" / "cmos_cells.lib"
        _, c = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--frontend", "slang",
                          "--liberty", str(lib))
        for name, rep in (("a", a), ("b", b), ("c", c)):
            (self.tmp / f"{name}.json").write_text(json.dumps(rep))
        proc = subprocess.run(
            [sys.executable, str(TOOL / "compare_reports.py"), "--json",
             *(str(self.tmp / f"{n}.json") for n in "abc")],
            capture_output=True, text=True, check=True,
        )
        rows = json.loads(proc.stdout)
        self.assertEqual(rows[1]["not_comparable"], "different frontend")
        self.assertEqual(rows[2]["not_comparable"], "different metric")
        self.assertTrue(all(r["delta_pct_vs_baseline"] is None for r in rows[1:]))


def fake_report(area: float | None = None, transistors: int = 1000, frontend: str = "slang",
                cells: int = 1, flops: int = 0) -> dict:
    return {
        "status": "ok", "top": "t", "frontend_used": frontend, "liberty": None if area is None else "x.lib",
        "stats": {"num_cells": cells, "num_flops": flops, "area": area, "estimated_transistors": transistors},
    }


class CompareGateTests(unittest.TestCase):
    """compare_reports.py --max-regression / --min-improvement / --metric on synthetic reports; never reaches Yosys."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="synth_area_test_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def compare(self, reps: list[dict], *flags: str) -> subprocess.CompletedProcess:
        paths = []
        for i, rep in enumerate(reps):
            paths.append(self.tmp / f"r{i}.json")
            paths[-1].write_text(json.dumps(rep))
        return subprocess.run([sys.executable, str(TOOL / "compare_reports.py"), *flags, *map(str, paths)],
                              capture_output=True, text=True, check=False)

    def test_no_flag_never_fails(self) -> None:
        for flags in ((), ("--json",)):
            proc = self.compare([fake_report(100.0), fake_report(1000.0)], *flags)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")

    def test_exactly_limit_passes(self) -> None:
        for base, cand in ((100.0, 105.0), (0.7, 0.735), (2000, 2100)):
            area = [fake_report(base), fake_report(cand)]
            self.assertEqual(self.compare(area, "--max-regression", "5").returncode, 0, (base, cand))
            tr = [fake_report(transistors=base), fake_report(transistors=cand)]
            self.assertEqual(self.compare(tr, "--max-regression", "5").returncode, 0, (base, cand))
        self.assertEqual(self.compare([fake_report(0.7), fake_report(0.77)], "--max-regression", "10").returncode, 0)

    def test_over_limit_fails(self) -> None:
        proc = self.compare([fake_report(100.0), fake_report(99.0), fake_report(105.01)], "--max-regression", "5")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("r2.json: area regressed +5.01%", proc.stderr)
        self.assertNotIn("r1.json", proc.stderr)
        proc = self.compare([fake_report(transistors=1000), fake_report(transistors=1001)], "--json",
                            "--max-regression", "0")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)[1]["delta_pct_vs_baseline"], 0.1)
        self.assertIn("transistors regressed", proc.stderr)

    def test_json_regressed_field(self) -> None:
        reps = [fake_report(100.0), fake_report(105.0), fake_report(105.01), fake_report(90.0),
                fake_report(500.0, frontend="sv2v")]
        rows = json.loads(self.compare(reps, "--json").stdout)
        self.assertEqual([r["regressed"] for r in rows], [None] * 5)
        proc = self.compare(reps, "--json", "--max-regression", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual([r["regressed"] for r in json.loads(proc.stdout)], [False, False, True, False, None])
        proc = self.compare(reps[:4], "--json", "--max-regression", "5")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual([r["regressed"] for r in json.loads(proc.stdout)], [False, False, True, False])

    def test_improvements_pass(self) -> None:
        proc = self.compare([fake_report(100.0), fake_report(10.0), fake_report(99.99)], "--max-regression", "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

    def test_incomparable_candidates_fail_with_3(self) -> None:
        missing = fake_report()
        del missing["stats"]["estimated_transistors"]
        cases = (
            (fake_report(500.0, frontend="sv2v"), "different frontend"),
            ({**fake_report(90.0), "liberty": "other.lib"}, "different liberty"),
            (fake_report(transistors=900), "different metric"),
            ({"status": "failed", "stats": {}}, "failed"),
            ({**fake_report(90.0), "stats": {**fake_report(90.0)["stats"], "area_is_lower_bound": True}}, "partial"),
        )
        for cand, why in cases:
            for flags in ((), ("--json",)):
                proc = self.compare([fake_report(100.0), fake_report(99.0), cand], *flags, "--max-regression", "5")
                self.assertEqual(proc.returncode, 3, (why, flags, proc.stderr))
                self.assertIn(f"r2.json: not comparable to baseline ({why}", proc.stderr)
                self.assertNotIn("r1.json", proc.stderr)
                if flags:
                    self.assertEqual(len(json.loads(proc.stdout)), 3)
        proc = self.compare([fake_report(), missing], "--json", "--max-regression", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("r1.json: not comparable to baseline (transistors is missing)", proc.stderr)
        self.assertEqual(json.loads(proc.stdout)[1]["not_comparable"], "transistors is missing")
        # a bad baseline makes every candidate incomparable, and wins over a regression elsewhere
        proc = self.compare([{"status": "failed", "stats": {}}, fake_report(100.0)], "--max-regression", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("r1.json: not comparable to baseline (baseline failed)", proc.stderr)
        proc = self.compare([fake_report(100.0), fake_report(200.0), fake_report(1.0, frontend="sv2v")], "--json",
                            "--max-regression", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("r1.json: area regressed", proc.stderr)
        self.assertIn("r2.json: not comparable", proc.stderr)
        # --allow-partial still makes partial area comparable
        partial = {**fake_report(90.0), "stats": {**fake_report(90.0)["stats"], "area_is_lower_bound": True}}
        proc = self.compare([fake_report(100.0), partial], "--allow-partial", "--max-regression", "5")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_flag_incomparable_unchanged(self) -> None:
        reps = [fake_report(100.0), fake_report(500.0, frontend="sv2v")]
        proc = self.compare(reps)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("not comparable", proc.stderr)
        proc = self.compare(reps, "--json")
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))

    def test_rejects_bad_limit(self) -> None:
        for bad in ("-1", "abc", "nan", "1e400"):
            for flag in ("--max-regression", "--min-improvement"):
                proc = self.compare([fake_report(100.0), fake_report(100.0)], flag, bad)
                self.assertEqual(proc.returncode, 2, (flag, bad))

    def test_metric_auto_is_the_default(self) -> None:
        area = [fake_report(100.0), fake_report(104.0), fake_report(106.0), fake_report(1.0, frontend="sv2v")]
        tr = [fake_report(transistors=1000), fake_report(transistors=1100)]
        for reps, explicit in ((area, "area"), (tr, "transistors")):
            for flags in ((), ("--json",), ("--max-regression", "5"), ("--json", "--max-regression", "5")):
                want = self.compare(reps, *flags)
                for m in ("auto", explicit):
                    got = self.compare(reps, *flags, "--metric", m)
                    self.assertEqual((got.returncode, got.stdout, got.stderr),
                                     (want.returncode, want.stdout, want.stderr), (m, flags))

    def test_metric_cells_and_flops(self) -> None:
        # no Liberty area: the transistor estimate (auto) shrank while the cell and flop counts grew
        reps = [fake_report(transistors=1000, cells=100, flops=10), fake_report(transistors=900, cells=106, flops=11)]
        self.assertEqual(self.compare(reps, "--max-regression", "5").returncode, 0)
        proc = self.compare(reps, "--metric", "cells", "--max-regression", "5")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("r1.json: cells regressed +6.00% vs baseline (limit 5%)", proc.stderr)
        proc = self.compare(reps, "--json", "--metric", "flops", "--max-regression", "10")
        self.assertEqual(proc.returncode, 0, proc.stderr)  # exactly +10% passes
        self.assertEqual([(r["metric"], r["value"], r["delta_pct_vs_baseline"], r["regressed"])
                          for r in json.loads(proc.stdout)], [("flops", 10, 0.0, False), ("flops", 11, 10.0, False)])
        # the chosen name stands where area/transistors does; the rest of the header is unchanged
        default = self.compare(reps).stdout.splitlines()[0]
        header = self.compare(reps, "--metric", "flops").stdout.splitlines()[0]
        self.assertEqual(header, default.replace(f"{'transistors':>14s}", f"{'flops':>14s}"))

    def test_counts_need_no_complete_area(self) -> None:
        # a library without any `area` leaves a zero, partial total; partial area blocks an area comparison only,
        # cell and flop counts are exact either way
        reps = [fake_report(0.0, cells=c, flops=4) for c in (100, 94, 106)]
        for rep in reps:
            rep["stats"]["area_is_lower_bound"] = True
        proc = self.compare(reps, "--max-regression", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("r1.json: not comparable to baseline (baseline area is zero", proc.stderr)
        proc = self.compare(reps, "--metric", "cells", "--max-regression", "5")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("r2.json: cells regressed +6.00%", proc.stderr)
        self.assertNotIn("not comparable", proc.stderr)
        self.assertEqual(self.compare(reps, "--metric", "flops", "--max-regression", "0").returncode, 0)
        partial = [fake_report(50.0, cells=10), fake_report(45.0, cells=10)]
        partial[1]["stats"]["area_is_lower_bound"] = True
        proc = self.compare(partial, "--metric", "area", "--max-regression", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("r1.json: not comparable to baseline (partial area", proc.stderr)
        self.assertEqual(self.compare(partial, "--metric", "cells", "--max-regression", "5").returncode, 0)
        # ...but counts still have to come from the same library and frontend
        for other, why in (({**partial[1], "liberty": "other.lib"}, "different liberty"),
                           ({**partial[1], "frontend_used": "sv2v"}, "different frontend")):
            proc = self.compare([partial[0], other], "--metric", "cells", "--max-regression", "5")
            self.assertEqual(proc.returncode, 3, why)
            self.assertIn(f"r1.json: not comparable to baseline ({why})", proc.stderr)

    def test_metric_missing_from_reports(self) -> None:
        # an explicit --metric the reports do not carry leaves no percentage to take, as it does for auto
        no_tr = fake_report(100.0)
        del no_tr["stats"]["estimated_transistors"]
        for m, reps, why in (("area", [fake_report(), fake_report()], "baseline area is missing"),
                             ("transistors", [no_tr, no_tr], "baseline transistors is missing"),
                             ("flops", [fake_report(flops=0), fake_report(flops=3)], "baseline flops is zero")):
            proc = self.compare(reps, "--json", "--metric", m, "--max-regression", "5")
            self.assertEqual(proc.returncode, 3, m)
            self.assertIn(f"r1.json: not comparable to baseline ({why}, no percentage is defined)", proc.stderr)
            self.assertEqual([r["metric"] for r in json.loads(proc.stdout)], [m, m])
        no_cells = fake_report(cells=5)
        del no_cells["stats"]["num_cells"]
        proc = self.compare([fake_report(cells=5), no_cells], "--json", "--metric", "cells", "--max-regression", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(json.loads(proc.stdout)[1]["not_comparable"], "cells is missing")
        self.assertEqual(self.compare([fake_report(), fake_report()], "--metric", "gates").returncode, 2)

    def test_min_improvement_exactly_limit_passes(self) -> None:
        for base, cand in ((100.0, 95.0), (0.7, 0.665), (2000, 1900)):
            area = [fake_report(base), fake_report(cand)]
            self.assertEqual(self.compare(area, "--min-improvement", "5").returncode, 0, (base, cand))
            tr = [fake_report(transistors=base), fake_report(transistors=cand)]
            self.assertEqual(self.compare(tr, "--min-improvement", "5").returncode, 0, (base, cand))
        self.assertEqual(self.compare([fake_report(0.7), fake_report(0.63)], "--min-improvement", "10").returncode, 0)
        cells = [fake_report(cells=200), fake_report(cells=190)]
        self.assertEqual(self.compare(cells, "--metric", "cells", "--min-improvement", "5").returncode, 0)
        # one improved candidate is enough, and at 0% a candidate level with the baseline counts
        proc = self.compare([fake_report(100.0), fake_report(130.0), fake_report(90.0)], "--min-improvement", "5")
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.compare([fake_report(100.0), fake_report(100.0)], "--min-improvement", "0").returncode, 0)

    def test_min_improvement_short_of_limit_fails_with_4(self) -> None:
        reps = [fake_report(100.0), fake_report(96.0), fake_report(95.01), fake_report(120.0)]
        proc = self.compare(reps, "--min-improvement", "5")
        self.assertEqual(proc.returncode, 4)
        self.assertRegex(proc.stderr,
                         r"no candidate improved area by at least 5% vs baseline \(best \S*r2\.json: -4\.99%\)")
        # --json rows are the same as without the flag ("regressed" stays null without --max-regression)
        proc = self.compare(reps, "--json", "--min-improvement", "5")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(json.loads(proc.stdout), json.loads(self.compare(reps, "--json").stdout))
        proc = self.compare([fake_report(cells=200), fake_report(cells=191)], "--metric", "cells",
                            "--min-improvement", "5")
        self.assertEqual(proc.returncode, 4)
        self.assertRegex(proc.stderr,
                         r"no candidate improved cells by at least 5% vs baseline \(best \S*r1\.json: -4\.50%\)")
        # the baseline is no candidate of its own
        proc = self.compare([fake_report(100.0)], "--min-improvement", "0")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("no candidate improved area by at least 0% vs baseline\n", proc.stderr)

    def test_min_improvement_precedence(self) -> None:
        # 1 wins over 4 and 3 wins over both; every reason is still named on stderr
        base, worse, flat, better = fake_report(100.0), fake_report(110.0), fake_report(100.0), fake_report(90.0)
        sv2v = fake_report(50.0, frontend="sv2v")
        both = ("--max-regression", "5", "--min-improvement", "5")
        proc = self.compare([base, worse, flat], *both)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("r1.json: area regressed +10.00%", proc.stderr)
        self.assertIn("no candidate improved area", proc.stderr)
        self.assertEqual(self.compare([base, worse, better], *both).returncode, 1)
        self.assertEqual(self.compare([base, flat], *both).returncode, 4)
        self.assertEqual(self.compare([base, flat, better], *both).returncode, 0)
        proc = self.compare([base, worse, flat, sv2v], *both)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("r3.json: not comparable to baseline (different frontend)", proc.stderr)
        self.assertIn("r1.json: area regressed", proc.stderr)
        self.assertIn("no candidate improved area", proc.stderr)
        # --min-improvement alone gates too: an incomparable candidate exits 3 (also with --json) even next to an
        # improved one, and never counts as the improvement itself
        for flags in ((), ("--json",)):
            proc = self.compare([base, better, sv2v], *flags, "--min-improvement", "5")
            self.assertEqual(proc.returncode, 3, flags)
            self.assertIn("r2.json: not comparable to baseline (different frontend)", proc.stderr)
        proc = self.compare([base, sv2v], "--min-improvement", "5")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("no candidate improved area", proc.stderr)
        missing = fake_report()
        del missing["stats"]["estimated_transistors"]
        proc = self.compare([fake_report(), missing], "--json", "--min-improvement", "5")
        self.assertEqual(proc.returncode, 3)
        row = json.loads(proc.stdout)[1]
        self.assertEqual((row["not_comparable"], row["regressed"]), ("transistors is missing", None))


class InvocationTests(unittest.TestCase):
    """Argument and tool-lookup handling; these never reach Yosys and run on an unbuilt checkout."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="synth_area_test_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_missing_source(self) -> None:
        code, rep = run_runner(self.tmp, "x", [self.tmp / "nope.sv"])
        self.assertEqual(code, 2)
        self.assertIn("source not found", rep["errors"][0])

    def test_bad_yosys_path_is_invocation_error(self) -> None:
        code, rep = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--yosys", str(self.tmp / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("yosys binary not found", rep["errors"][0])

    def test_non_executable_yosys_is_a_reported_failure(self) -> None:
        fake = self.tmp / "yosys"
        fake.write_text("not a binary\n")
        fake.chmod(0o644)
        out = self.tmp / "r.json"
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--top", "sync_fifo", "-o", str(out), "-q", "--yosys", str(fake),
             "--frontend", "slang", str(EXAMPLES / "sync_fifo.sv")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        rep = json.loads(out.read_text())
        self.assertEqual(rep["status"], "failed")
        self.assertIn("Permission denied", rep["stages"][0]["stderr_tail"])

    def test_top_must_be_identifier(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--top", "x; shell rm -rf /", str(EXAMPLES / "sync_fifo.sv")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("plain module identifier", proc.stderr)


@unittest.skipUnless(YOSYS, "yosys binary not found")
@unittest.skipUnless(shutil.which("bash"), "bash not found")
class SlurmWrapperTests(unittest.TestCase):
    def test_local_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.json"
            proc = subprocess.run(
                ["bash", str(TOOL / "slurm_synth_area.sh"), "--top", "sync_fifo", "-o", str(out), "-q",
                 str(EXAMPLES / "sync_fifo.sv")],
                capture_output=True, text=True, check=False,
                env={**os.environ, "SYNTH_AREA_LOCAL": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(out.read_text())["status"], "ok")

    def test_sbatch_submission_and_sweep(self) -> None:
        """Fake sbatch: record flags, run the --wrap command, print a job id."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake = tmp / "bin"
            fake.mkdir()
            (fake / "sbatch").write_text(
                "#!/usr/bin/env bash\n"
                'for a in "$@"; do case "$a" in --wrap=*) WRAP="${a#--wrap=}";; *) echo "$a" >> "$SBATCH_LOG";; esac; done\n'
                'bash -c "$WRAP" >/dev/null || exit 1\n'
                "echo 4242\n"
            )
            (fake / "sbatch").chmod(0o755)
            log = tmp / "sbatch_args.txt"
            env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "SBATCH_LOG": str(log),
                   "SYNTH_AREA_PARTITION": "cpu", "SYNTH_AREA_SBATCH_ARGS": "--account=chip --qos=fast",
                   "SYNTH_AREA_LOGDIR": str(tmp / "slurm_logs")}
            env.pop("SYNTH_AREA_LOCAL", None)

            blocks = tmp / "blocks.txt"
            # deliberately no trailing newline, and a line that looks like an option
            blocks.write_text(
                f"sync_fifo {EXAMPLES / 'sync_fifo.sv'}\n"
                "# comment\n"
                f"struct_fifo {EXAMPLES / 'fifo_pkg.sv'} {EXAMPLES / 'struct_fifo.sv'}"
            )
            proc = subprocess.run(
                ["bash", str(TOOL / "slurm_sweep.sh"), str(blocks), str(tmp / "out"), "--frontend", "slang", "-q"],
                capture_output=True, text=True, check=False, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.split(), ["4242", "4242"])
            flags = log.read_text().split()
            self.assertIn("--partition=cpu", flags)
            self.assertIn("--account=chip", flags)
            self.assertIn("--qos=fast", flags)
            self.assertIn("--parsable", flags)
            for top in ("sync_fifo", "struct_fifo"):
                rep = json.loads((tmp / "out" / f"{top}.json").read_text())
                self.assertEqual(rep["status"], "ok", rep["errors"])
                self.assertEqual(rep["frontend_used"], "slang")


if __name__ == "__main__":
    unittest.main(verbosity=2)
