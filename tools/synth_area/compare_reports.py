#!/usr/bin/env python3
"""
Compare synth_area JSON reports side by side (baseline first).

    compare_reports.py baseline.json candidate1.json [candidate2.json ...]

Prints cells / flops / area (or transistor estimate) and the delta vs. the
baseline, so an optimizer loop can read "did this change make the block
smaller" from one line. --json emits the same as machine-readable output.
--metric picks what the delta is taken on: auto (Liberty area when the reports
have it, else the transistor estimate), area, transistors, cells or flops.
--max-regression PCT makes it a CI gate: exit 1 if any candidate's metric grew
by more than PCT percent over the baseline, exit 3 if any candidate is not
comparable to the baseline (failed, different frontend/liberty/metric, ...).
--min-improvement PCT: exit 4 if no candidate's metric is at least PCT percent
below the baseline; 1 wins over 4, and 3 wins over both.
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

METRICS = ("auto", "area", "transistors", "cells", "flops")
STATS_KEYS = {"transistors": "estimated_transistors", "cells": "num_cells", "flops": "num_flops"}


def metric(rep: dict, which: str = "auto") -> tuple[str, float | None]:
    s = rep.get("stats", {})
    if which == "auto":
        which = "area" if s.get("area") is not None else "transistors"
    if which == "area":
        return "area", None if s.get("area") is None else float(s["area"])
    return which, s.get(STATS_KEYS[which])


def incomparable(base: dict, rep: dict, allow_partial: bool, which: str = "auto") -> str | None:
    """Why `rep` must not be ranked against `base` on metric `which`, or None if it may be."""
    if rep.get("status") != "ok":
        return "failed"
    if base.get("status") != "ok":
        return "baseline failed"
    base_name, base_val = metric(base, which)
    if not base_val:
        return f"baseline {base_name} is {'missing' if base_val is None else 'zero'}, no percentage is defined"
    if metric(rep, which)[0] != base_name:
        return "different metric"
    if rep.get("liberty") != base.get("liberty"):
        return "different liberty"
    if rep.get("frontend_used") != base.get("frontend_used"):
        return "different frontend"
    # the generic transistor estimate is a lower bound for any design with async-reset flops, and cell and
    # flop counts are exact either way, so only Liberty area (where unknown cells are usually macros) blocks comparison
    partial = any(r.get("stats", {}).get("area_is_lower_bound") for r in (base, rep))
    if base_name == "area" and not allow_partial and partial:
        return "partial area (cell types missing from liberty; pass --allow-partial)"
    return None


def percent(text: str) -> Fraction:
    try:
        pct = Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if pct < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0: {text!r}")
    try:
        float(pct)
    except OverflowError:
        raise argparse.ArgumentTypeError(f"too large: {text!r}") from None
    return pct


def regressed(base_val: float, val: float, max_pct: Fraction) -> bool:
    """True if `val` exceeds `base_val` by more than `max_pct` percent, in exact decimal arithmetic."""
    base, cand = Fraction(repr(base_val)), Fraction(repr(val))
    return (cand - base) * 100 > max_pct * base


def improved(base_val: float, val: float, min_pct: Fraction) -> bool:
    """True if `val` is at least `min_pct` percent below `base_val`, in exact decimal arithmetic."""
    base, cand = Fraction(repr(base_val)), Fraction(repr(val))
    return (base - cand) * 100 >= min_pct * base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-partial", action="store_true",
                    help="compute deltas even when some cells had no area/transistor model")
    ap.add_argument("--metric", choices=METRICS, default="auto",
                    help="what to compare and gate on: auto (default) is Liberty area when the reports have it, "
                         "else the transistor estimate; cells / flops also work without Liberty area")
    ap.add_argument("--max-regression", type=percent, metavar="PCT",
                    help="exit 1 if any candidate's metric is more than PCT percent above the baseline, "
                         "3 if any candidate is not comparable to the baseline")
    ap.add_argument("--min-improvement", type=percent, metavar="PCT",
                    help="exit 4 if no candidate's metric is at least PCT percent below the baseline "
                         "(1 and 3 win over 4)")
    args = ap.parse_args()

    reps = [json.loads(Path(p).read_text()) for p in args.reports]
    base = reps[0]
    base_name, base_val = metric(base, args.metric)
    gate = args.max_regression is not None or args.min_improvement is not None
    rows = []
    regressions = []
    ungated = []
    improvements = []
    best = None  # (delta, path) of the comparable candidate furthest below the baseline
    for i, (path, rep) in enumerate(zip(args.reports, reps)):
        name, val = metric(rep, args.metric)
        s = rep.get("stats", {})
        why = incomparable(base, rep, args.allow_partial, args.metric)
        delta = None
        worse = None
        if why is None and val is not None:
            delta = (val - base_val) / base_val * 100.0
            if args.max_regression is not None:
                worse = regressed(base_val, val, args.max_regression)
                if i > 0 and worse:
                    regressions.append((path, delta))
            if i > 0 and args.min_improvement is not None:
                best = min(best or (delta, path), (delta, path))
                if improved(base_val, val, args.min_improvement):
                    improvements.append(path)
        elif gate and i > 0 and why is None:
            why = f"{name} is missing"
        if why and (gate or not args.json):
            print(f"compare_reports: {path}: not comparable to baseline ({why})", file=sys.stderr)
        if i > 0 and gate and delta is None:
            ungated.append(path)
        rows.append({
            "report": path,
            "status": rep.get("status"),
            "top": rep.get("top"),
            "frontend": rep.get("frontend_used"),
            "cells": s.get("num_cells"),
            "flops": s.get("num_flops"),
            "metric": name,
            "value": val,
            "delta_pct_vs_baseline": None if delta is None else round(delta, 2),
            "not_comparable": why,
            "regressed": worse,
            "seconds": rep.get("wall_seconds"),
        })

    for path, delta in regressions:
        print(f"compare_reports: {path}: {base_name} regressed {delta:+.2f}% vs baseline "
              f"(limit {float(args.max_regression):g}%)", file=sys.stderr)
    unimproved = args.min_improvement is not None and not improvements
    if unimproved:
        print(f"compare_reports: no candidate improved {base_name} by at least {float(args.min_improvement):g}% "
              "vs baseline" + ("" if best is None else f" (best {best[1]}: {best[0]:+.2f}%)"), file=sys.stderr)
    status = 3 if ungated else 1 if regressions else 4 if unimproved else 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return status

    hdr = f"{'report':40s} {'status':7s} {'cells':>7s} {'flops':>6s} {base_name:>14s} {'delta%':>8s} {'sec':>6s}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        v = "-" if r["value"] is None else f"{r['value']:.1f}"
        d = "-" if r["delta_pct_vs_baseline"] is None else f"{r['delta_pct_vs_baseline']:+.2f}"
        print(f"{Path(r['report']).name[:40]:40s} {r['status']!s:7s} {r['cells']!s:>7s} "
              f"{r['flops']!s:>6s} {v:>14s} {d:>8s} {r['seconds']!s:>6s}")
    return status


if __name__ == "__main__":
    sys.exit(main())
