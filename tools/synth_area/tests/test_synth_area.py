#!/usr/bin/env python3
"""
Smoke tests for the synth_area runner. Needs a built yosys (./build/yosys,
$YOSYS or on PATH); sv2v tests are skipped when sv2v is not installed.

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

    def test_missing_source(self) -> None:
        code, rep = run_runner(self.tmp, "x", [self.tmp / "nope.sv"])
        self.assertEqual(code, 2)
        self.assertIn("source not found", rep["errors"][0])

    def test_bad_yosys_path_is_invocation_error(self) -> None:
        code, rep = run_runner(self.tmp, "sync_fifo", [EXAMPLES / "sync_fifo.sv"], "--yosys", str(self.tmp / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("yosys binary not found", rep["errors"][0])

    def test_top_must_be_identifier(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--top", "x; shell rm -rf /", str(EXAMPLES / "sync_fifo.sv")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("plain module identifier", proc.stderr)

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
