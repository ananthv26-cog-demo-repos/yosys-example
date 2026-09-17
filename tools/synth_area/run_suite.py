#!/usr/bin/env python3
"""
run_suite: run bool_area.py over the RTL corpus described by a suite manifest and check
the hand-counted expectations.

    python3 run_suite.py corpus/suite.json -o out/suite

Manifest entries:
    {"name": "half_adder", "sources": ["corpus/half_adder.sv"], "top": "half_adder",
     "expect": {"gate_total": 2, "dff": 0, "max_depth": 1}}

`expect` values are compared exactly against `metrics.json` -> `summary`; `expect_max`
gives upper bounds. Any block whose flow fails or whose expectations do not hold makes the
suite exit nonzero. A `suite_summary.json` and a Markdown table are written to the
output directory so the numbers can be pasted into a report.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE_COLS = ("gate_total", "dff", "edge_total", "max_depth", "max_fanout", "mapped_cell_total", "mapped_cell_area")


def run_block(entry: dict, out_dir: Path, extra: list[str], python: str) -> dict:
    sources = [str((HERE / s).resolve()) for s in entry["sources"]]
    block_out = out_dir / entry["name"]
    cmd = [python, str(HERE / "bool_area.py"), *sources, "--top", entry["top"], "-o", str(block_out), "-q", *extra]
    for inc in entry.get("include", []):
        cmd += ["-I", str((HERE / inc).resolve())]
    for d in entry.get("define", []):
        cmd += ["-D", d]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    res = {"name": entry["name"], "top": entry["top"], "exit_code": p.returncode, "seconds": round(time.time() - t0, 3),
           "out_dir": str(block_out), "checks": []}
    metrics_path = block_out / "metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else None
    res["status"] = metrics["status"] if metrics else "no-metrics"
    res["stage"] = metrics["stage"] if metrics else None
    res["errors"] = (metrics["errors"] if metrics else []) or ([p.stderr.strip()] if p.returncode else [])
    summary = (metrics or {}).get("summary") or {}
    res["summary"] = {k: summary.get(k) for k in TABLE_COLS}
    res["equivalence"] = ((metrics or {}).get("equivalence") or {}).get("status")
    res["simulation"] = ((metrics or {}).get("simulation") or {}).get("status")
    ok = p.returncode == 0 and res["status"] == "ok"
    for key, want in entry.get("expect", {}).items():
        got = summary.get(key)
        passed = got == want
        res["checks"].append({"key": key, "op": "==", "want": want, "got": got, "passed": passed})
        ok &= passed
    for key, bound in entry.get("expect_max", {}).items():
        got = summary.get(key)
        passed = got is not None and got <= bound
        res["checks"].append({"key": key, "op": "<=", "want": bound, "got": got, "passed": passed})
        ok &= passed
    res["passed"] = ok
    return res


def markdown_table(results: list[dict]) -> str:
    head = ["block", "status", "equiv", "sim", *TABLE_COLS, "checks", "s"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in results:
        checks = f"{sum(c['passed'] for c in r['checks'])}/{len(r['checks'])}" if r["checks"] else "-"
        cells = [r["name"], "PASS" if r["passed"] else f"FAIL({r['stage']})", str(r["equivalence"]), str(r["simulation"]),
                 *[str(r["summary"][c]) for c in TABLE_COLS], checks, str(r["seconds"])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", nargs="?", default=str(HERE / "corpus" / "suite.json"))
    ap.add_argument("-o", "--out-dir", required=True)
    ap.add_argument("--only", action="append", default=[], help="run only these block names")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("extra", nargs="*", help="extra arguments passed to bool_area.py (after --)")
    args = ap.parse_args(argv)

    manifest = json.loads(Path(args.manifest).read_text())
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = [e for e in manifest["blocks"] if not args.only or e["name"] in args.only]
    results = []
    for e in entries:
        r = run_block(e, out_dir, args.extra, args.python)
        results.append(r)
        flag = "ok  " if r["passed"] else "FAIL"
        s = r["summary"]
        detail = (f"gates={s['gate_total']} dffs={s['dff']} depth={s['max_depth']} cells={s['mapped_cell_total']} "
                  f"area={s['mapped_cell_area']}") if r["status"] == "ok" else "; ".join(r["errors"])[:200]
        bad = [c for c in r["checks"] if not c["passed"]]
        if bad:
            detail += " | expectation failed: " + ", ".join(f"{c['key']} {c['op']} {c['want']} (got {c['got']})" for c in bad)
        print(f"[suite] {flag} {r['name']:<22} {detail} ({r['seconds']}s)")
    passed = sum(r["passed"] for r in results)
    summary = {"manifest": str(Path(args.manifest).resolve()), "blocks": len(results), "passed": passed,
               "failed": len(results) - passed, "results": results}
    (out_dir / "suite_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out_dir / "suite_table.md").write_text(markdown_table(results))
    print(f"[suite] {passed}/{len(results)} blocks passed -> {out_dir / 'suite_summary.json'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
