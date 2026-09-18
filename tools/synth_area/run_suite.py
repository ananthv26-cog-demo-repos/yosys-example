#!/usr/bin/env python3
"""
run_suite: run bool_area.py over the RTL corpus described by a suite manifest and check
the hand-counted expectations.

    python3 run_suite.py corpus/suite.json -o out/suite

Manifest entries:
    {"name": "half_adder", "sources": ["corpus/half_adder.sv"], "top": "half_adder",
     "expect": {"gate_total": 2, "dff": 0, "max_depth": 1}}

Relative `sources` and `include` paths resolve against this tool's directory (so the shipped
manifest can mix `corpus/` and `examples/`); use absolute paths in manifests kept elsewhere.
Extra arguments after `--` are appended to every bool_area.py command line after the
manifest's own options, so they win where bool_area.py takes the last value (e.g.
`--sim-cycles`).

`expect` values are compared exactly against `metrics.json` -> `summary`; `expect_max`
gives upper bounds. Any block whose flow fails or whose expectations do not hold makes the
suite exit nonzero. `suite_summary.json`, a Markdown table, and the reviewer rollup
(`suite_evidence.json` / `EVIDENCE.md`, one line per PRD success criterion) are written to
the output directory so the numbers can be pasted into a report.

Blocks are independent processes, so `-j` runs them concurrently (`-j0` = one per core);
the output files stay in manifest order regardless, though the console lines appear in
completion order. A `--baseline` mismatch fails the run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import suite_evidence
from bool_area import purge_outputs

TABLE_COLS = ("gate_total", "dff", "edge_total", "max_depth", "max_fanout", "mapped_cell_total", "mapped_cell_area")


def metrics_schema_error(metrics: object) -> str | None:
    """Why `metrics` does not have the bool_area.py shape run_block reads, or None if it does."""
    if not isinstance(metrics, dict):
        return "not a JSON object"
    for key, typ in (("status", str), ("stage", (str, type(None))), ("errors", list)):
        if not isinstance(metrics.get(key), typ):
            return f"missing or malformed '{key}'"
    if not all(isinstance(e, str) for e in metrics["errors"]):
        return "'errors' has a non-string entry"
    for key in ("summary", "equivalence", "simulation"):
        if not isinstance(metrics.get(key), (dict, type(None))):
            return f"'{key}' is not an object"
    return None


def run_block(entry: dict, out_dir: Path, extra: list[str], python: str) -> dict:
    sources = [str((HERE / s).resolve()) for s in entry["sources"]]
    block_out = out_dir / entry["name"]
    cmd = [python, str(HERE / "bool_area.py"), *sources, "--top", entry["top"], "-o", str(block_out), "-q"]
    for inc in entry.get("include", []):
        cmd += ["-I", str((HERE / inc).resolve())]
    for d in entry.get("define", []):
        cmd += ["-D", d]
    cmd += [*entry.get("args", []), *extra]
    t0 = time.time()
    res = {"name": entry["name"], "top": entry["top"], "exit_code": None, "seconds": None, "out_dir": str(block_out),
           "checks": []}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as e:
        errors = [f"cannot run {cmd[0]}: {e}"]
        try:
            if block_out.is_dir():
                purge_outputs(block_out)
        except OSError as e2:
            errors.append(f"stale artifacts from an earlier run are left in {block_out}: {e2}")
        res.update(seconds=round(time.time() - t0, 3), status="failed", stage="launch", errors=errors,
                   summary={k: None for k in TABLE_COLS}, equivalence=None, simulation=None, passed=False)
        return res
    res.update(exit_code=p.returncode, seconds=round(time.time() - t0, 3))
    metrics_path = block_out / "metrics.json"
    metrics, bad_metrics = None, None
    try:
        metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else None
    except (OSError, ValueError) as e:
        metrics, bad_metrics = None, f"unreadable {metrics_path}: {e}"
    if metrics is not None and (schema_error := metrics_schema_error(metrics)):
        metrics, bad_metrics = None, f"unreadable {metrics_path}: {schema_error}"
    res["status"] = metrics["status"] if metrics else ("bad-metrics" if bad_metrics else "no-metrics")
    res["stage"] = metrics["stage"] if metrics else None
    res["errors"] = (metrics["errors"] if metrics else [bad_metrics] if bad_metrics else []) or (
        [p.stderr.strip()] if p.returncode else [])
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
        passed = isinstance(got, (int, float)) and got <= bound
        res["checks"].append({"key": key, "op": "<=", "want": bound, "got": got, "passed": passed})
        ok &= passed
    res["passed"] = ok
    return res


def block_detail(r: dict) -> str:
    s = r["summary"]
    detail = (f"gates={s['gate_total']} dffs={s['dff']} depth={s['max_depth']} cells={s['mapped_cell_total']} "
              f"area={s['mapped_cell_area']}") if r["status"] == "ok" else "; ".join(r["errors"])[:200]
    bad = [c for c in r["checks"] if not c["passed"]]
    if bad:
        detail += " | expectation failed: " + ", ".join(f"{c['key']} {c['op']} {c['want']} (got {c['got']})" for c in bad)
    return detail


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
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="blocks to run concurrently (0 = one per core); each block is its own process")
    ap.add_argument("--baseline", help="an earlier suite_evidence.json to check layer files against for determinism")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("extra", nargs="*", help="extra arguments passed to bool_area.py (after --)")
    argv = sys.argv[1:] if argv is None else list(argv)
    # Split at "--" ourselves: older argparse (Python 3.10) rejects option-like tokens after "--" in a "*" positional.
    extra: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1:]
    args = ap.parse_args(argv)
    args.extra += extra

    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text())
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = [e for e in manifest["blocks"] if not args.only or e["name"] in args.only]
    unknown = sorted(set(args.only or []) - {e["name"] for e in manifest["blocks"]})
    if unknown:
        ap.error(f"--only names not in the manifest: {', '.join(unknown)}")
    if args.jobs < 0:
        ap.error("--jobs must be 0 (one per core) or a positive number of blocks")
    # a name is also an output directory, so duplicates would have two concurrent blocks purging
    # and rewriting the same artifacts
    dupes = sorted({e["name"] for e in entries if [x["name"] for x in entries].count(e["name"]) > 1})
    if dupes:
        ap.error(f"duplicate block names in the manifest: {', '.join(dupes)}")
    jobs = max(1, min(args.jobs or (os.cpu_count() or 1), len(entries) or 1))
    baseline_path = Path(args.baseline).resolve() if args.baseline else None
    if baseline_path:  # read it now: a bad path must not surface after a full corpus run
        try:
            suite_evidence.load_baseline(baseline_path)
        except ValueError as e:
            ap.error(f"--baseline {e}")

    def run_and_report(e: dict) -> dict:
        r = run_block(e, out_dir, args.extra, args.python)
        print(f"[suite] {'ok  ' if r['passed'] else 'FAIL'} {r['name']:<22} {block_detail(r)} ({r['seconds']}s)",
              flush=True)
        return r

    t0 = time.time()
    if jobs == 1:
        results = [run_and_report(e) for e in entries]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:  # each block is a subprocess; threads only wait on them
            results = list(pool.map(run_and_report, entries))
    passed = sum(r["passed"] for r in results)
    summary = {"manifest": str(manifest_path), "blocks": len(results), "passed": passed,
               "failed": len(results) - passed, "jobs": jobs, "wall_seconds": round(time.time() - t0, 3),
               "results": results}
    suite_evidence.write_atomic(out_dir / "suite_summary.json", json.dumps(summary, indent=2) + "\n")
    suite_evidence.write_atomic(out_dir / "suite_table.md", markdown_table(results))
    evidence = suite_evidence.write(results, out_dir, baseline_path)
    for c in evidence["criteria"]:
        if c["ok"] is not True:
            print(f"[suite] criterion {'UNKNOWN' if c['ok'] is None else 'FAILED'}: {c['id']} — {c['measured']}")
    print(f"[suite] {passed}/{len(results)} blocks passed in {summary['wall_seconds']}s (-j{jobs}), "
          f"{evidence['criteria_passed']}/{evidence['criteria_total']} criteria -> {out_dir / 'EVIDENCE.md'}")
    # corpus-wide criteria can legitimately fail for an --only selection, but a baseline was asked
    # for explicitly: layer files that changed make the run fail
    deterministic = next(c["ok"] for c in evidence["criteria"] if c["id"] == "determinism")
    return 0 if passed == len(results) and (not args.baseline or deterministic is True) else 1


if __name__ == "__main__":
    sys.exit(main())
