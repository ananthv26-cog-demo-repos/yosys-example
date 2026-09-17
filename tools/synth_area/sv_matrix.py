#!/usr/bin/env python3
"""
Run every probe in sv_probes/ (and the FIFO examples) through each frontend and
tabulate what works. Produces a Markdown table (stdout or --md) and a JSON dump.

    sv_matrix.py --md SV_SUPPORT_MATRIX.md --json matrix.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "synth_area.py"
FRONTENDS = ("slang", "sv2v", "verilog")

# (label, top, [sources]) for the multi-file FIFO examples
EXAMPLES = [
    ("sync_fifo", "sync_fifo", ["examples/sync_fifo.sv"]),
    ("struct_fifo (pkg+struct+enum)", "struct_fifo", ["examples/fifo_pkg.sv", "examples/struct_fifo.sv"]),
    ("if_fifo_top (interfaces)", "if_fifo_top", ["examples/if_fifo.sv"]),
    ("async_fifo (2 clocks, gray)", "async_fifo", ["examples/async_fifo.sv"]),
]


def probe_description(path: Path) -> str:
    first = path.read_text().splitlines()[0]
    return first.lstrip("/ ").strip()


def one(top: str, sources: list[str], frontend: str, tmp: Path, extra: list[str]) -> dict:
    out = tmp / f"{top}.{frontend}.json"
    cmd = [sys.executable, str(RUNNER), "--top", top, "--frontend", frontend, "-o", str(out), "-q", *extra, *sources]
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, check=False)
    try:
        rep = json.loads(out.read_text())
    except (OSError, ValueError) as ex:
        rep = {"status": "failed", "stats": {},
               "errors": [f"runner produced no report (exit {proc.returncode}): {ex}; {proc.stderr.strip()[-200:]}"]}
    err = rep["errors"][0] if rep["errors"] else ""
    err = re.sub(r"\s+", " ", err)
    err = re.sub(r"^\[[a-z0-9]+\] ", "", err)
    err = re.sub(r"(\.\./)+home/\S+/sv_probes/", "", err)
    err = re.sub(r"/\S+/(sv_probes|examples)/", "", err)
    err = re.sub(r"/tmp/\S+\.work/", "", err)
    return {
        "ok": rep["status"] == "ok",
        "cells": rep["stats"].get("num_cells"),
        "flops": rep["stats"].get("num_flops"),
        "error": err[:160],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", help="write markdown table here")
    ap.add_argument("--json", help="write raw results here")
    ap.add_argument("--liberty", help="pass a liberty file through to the runner")
    ap.add_argument("--frontends", default=",".join(FRONTENDS))
    args = ap.parse_args()
    frontends = args.frontends.split(",")
    extra = ["--liberty", args.liberty] if args.liberty else []

    cases = [(f"{p.stem}: {probe_description(p)}", p.stem, [str(p.relative_to(HERE))])
             for p in sorted((HERE / "sv_probes").glob("*.sv"))]
    cases += EXAMPLES

    results = []
    with tempfile.TemporaryDirectory(prefix="sv_matrix_") as td:
        tmp = Path(td)
        for label, top, srcs in cases:
            row = {"label": label, "top": top, "sources": srcs, "results": {}}
            for fe in frontends:
                row["results"][fe] = one(top, srcs, fe, tmp, extra)
            results.append(row)
            print(f"{top:32s} " + " ".join(
                f"{fe}={'ok' if row['results'][fe]['ok'] else 'FAIL'}" for fe in frontends), file=sys.stderr)

    def cell(r: dict) -> str:
        if r["ok"]:
            return f"ok ({r['cells']} cells)"
        return "FAIL: " + r["error"].replace("|", "\\|") if r["error"] else "FAIL"

    lines = ["| construct | " + " | ".join(frontends) + " |", "|---|" + "---|" * len(frontends)]
    for row in results:
        lines.append(f"| {row['label']} | " + " | ".join(cell(row["results"][fe]) for fe in frontends) + " |")
    totals = " | ".join(f"{sum(1 for r in results if r['results'][fe]['ok'])}/{len(results)}" for fe in frontends)
    lines.append(f"| **passing** | {totals} |")
    md = "\n".join(lines) + "\n"

    if args.md:
        Path(args.md).write_text(md)
    else:
        print(md)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
