#!/usr/bin/env python3
"""
suite_evidence: turn one suite run into the evidence a reviewer actually has to read.

A suite run leaves 49 directories of per-block artifacts. This rolls them up into two files
next to `suite_summary.json`:

    suite_evidence.json   per-block layer presence, sha256, equivalence, simulation and
                          source-location coverage, plus one pass/fail line per criterion
    EVIDENCE.md           the same as a one-page table

Determinism is the one criterion a single run cannot show, so it stays `unknown` unless the
run is compared against an earlier `suite_evidence.json`:

    python3 run_suite.py -o out/run2 --baseline out/run1/suite_evidence.json

which reports whether every layer file of every shared block came out byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path

EVIDENCE_SCHEMA_VERSION = 1

# layer name -> the artifact key run_manifest.json records it under
LAYERS = {"word_level": "word", "sequential_overlay": "sequential", "boolean_graph": "graph",
          "mapped_cells": "mapped_cells"}
RUN_ARTIFACTS = ("metrics", "manifest", "summary")
MIN_FIFO_VARIANTS = 20
# RTL constructs keep their source location; cells Yosys invents while lowering (mux trees for
# `case`, carry chains) and everything ABC restructures do not, so only registers are required to
# be fully covered and mapped cells are reported for information only.
MIN_WORD_LEVEL_SRC = 0.9


def read_object(path: Path) -> dict:
    """A JSON object from `path`, or {} if it is missing, unreadable or not an object: a block whose
    artifacts are broken must show up as missing evidence, not as a crashed rollup."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def src_coverage(path: Path, key: str) -> tuple[int, int]:
    """(items carrying a `src` string, items) for a layer file, (0, 0) if it cannot be read."""
    items = read_object(path).get(key)
    if not isinstance(items, list):
        return 0, 0
    return sum(bool(i.get("src")) for i in items if isinstance(i, dict)), len(items)


def block_evidence(result: dict) -> dict:
    """Evidence for one `run_suite.run_block` result, read back from the block's artifacts."""
    out_dir = Path(result["out_dir"])
    ev: dict = {"name": result["name"], "passed": result["passed"], "status": result["status"],
                "seconds": result["seconds"], "errors": result["errors"]}
    artifacts = read_object(out_dir / "run_manifest.json").get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}

    def entry(key: str) -> dict:
        e = artifacts.get(key)
        return e if isinstance(e, dict) else {}
    ev["artifacts"] = {name: {"present": bool(entry(key).get("exists")), "sha256": entry(key).get("sha256")}
                       for name, key in list(LAYERS.items()) + [(k, k) for k in RUN_ARTIFACTS]}
    ev["layers_present"] = all(ev["artifacts"][name]["present"] for name in LAYERS)

    metrics = read_object(out_dir / "metrics.json")

    def section(container: dict, key: str) -> dict:
        v = container.get(key)
        return v if isinstance(v, dict) else {}
    mapped = section(metrics, "mapped")
    ev["mapped"] = {"cells": mapped.get("num_cells"), "area": mapped.get("area")}
    eq = section(metrics, "equivalence")
    ev["equivalence"] = {"status": eq.get("status"),
                         "checks": {n: {"status": c.get("status"), "proven": c.get("proven"),
                                        "unproven": c.get("unproven")}
                                    for n, c in section(eq, "checks").items() if isinstance(c, dict)}}
    sim = section(metrics, "simulation")
    ev["simulation"] = {k: sim.get(k) for k in ("status", "cycles", "compared_bits", "mismatches", "x_bits")}
    warnings = metrics.get("warnings")
    ev["warnings"] = len(warnings) if isinstance(warnings, list) else 0

    covered, total = {}, {}
    for layer, key in (("word_level", "operations"), ("sequential_overlay", "registers"), ("mapped_cells", "cells")):
        covered[layer], total[layer] = src_coverage(out_dir / f"{layer}.json", key)
    ev["src_coverage"] = {"with_src": covered, "items": total}
    return ev


def pct(num: int, den: int) -> str:
    return f"{num}/{den}" + (f" ({100 * num // den}%)" if den else "")


def determinism(blocks: list[dict], baseline: dict | None) -> dict:
    """Compare every block's layer hashes against an earlier suite_evidence.json."""
    if not baseline:
        return {"ok": None, "measured": "not checked (no --baseline)"}
    prev = {b["name"]: b.get("artifacts", {}) for b in baseline.get("blocks", []) if isinstance(b, dict)}
    shared = [b for b in blocks if b["name"] in prev]
    differing = [f"{b['name']}/{layer}" for b in shared for layer in LAYERS
                 if b["artifacts"][layer]["sha256"] != prev[b["name"]].get(layer, {}).get("sha256")]
    return {"ok": not differing and bool(shared), "differing": differing,
            "measured": (f"{len(shared)} blocks x {len(LAYERS)} layer files byte-identical to the baseline"
                         if not differing else f"{len(differing)} layer files differ: {', '.join(differing[:5])}")}


def criteria(blocks: list[dict], baseline: dict | None) -> list[dict]:
    """One entry per PRD success criterion: what it requires, what this run measured, pass/fail."""
    n = len(blocks)
    fifos = [b for b in blocks if "fifo" in b["name"].lower()]
    ok_blocks = [b for b in blocks if b["passed"]]
    with_layers = [b for b in blocks if b["layers_present"]]
    with_area = [b for b in blocks if (b["mapped"]["cells"] or 0) > 0 and (b["mapped"]["area"] or 0) > 0]
    eq_status = [b["equivalence"]["status"] for b in blocks]
    unproven = sum((c["unproven"] or 0) for b in blocks for c in b["equivalence"]["checks"].values()
                   if c["status"] == "failed")
    sims = [b["simulation"] for b in blocks if b["simulation"]["status"]]
    mismatches = sum(s["mismatches"] or 0 for s in sims)
    src = {layer: (sum(b["src_coverage"]["with_src"][layer] for b in blocks),
                   sum(b["src_coverage"]["items"][layer] for b in blocks))
           for layer in ("word_level", "sequential_overlay", "mapped_cells")}
    seq_src, word_src = src["sequential_overlay"], src["word_level"]
    src_ok = seq_src[1] > 0 and seq_src[0] == seq_src[1] and word_src[0] >= MIN_WORD_LEVEL_SRC * word_src[1]
    det = determinism(blocks, baseline)
    return [
        {"id": "blocks_pass", "requirement": "every corpus block runs end to end and meets its hand counts",
         "measured": f"{len(ok_blocks)}/{n} blocks", "ok": len(ok_blocks) == n},
        {"id": "fifo_variants", "requirement": f"at least {MIN_FIFO_VARIANTS} small FIFO variants",
         "measured": f"{len(fifos)} FIFO blocks", "ok": len(fifos) >= MIN_FIFO_VARIANTS},
        {"id": "all_layers", "requirement": "word-level, sequential, Boolean and mapped report for every design",
         "measured": f"{len(with_layers)}/{n} blocks with all four layer files",
         "ok": len(with_layers) == n},
        {"id": "asap7_area", "requirement": "ASAP7 mapping reports cell counts and summed area",
         "measured": f"{len(with_area)}/{n} blocks, {sum(b['mapped']['cells'] for b in with_area)} cells, "
                     f"{round(sum(b['mapped']['area'] for b in with_area), 5)} um2 total",
         "ok": len(with_area) == n},
        {"id": "nothing_omitted",
         "requirement": "no unsupported cell silently dropped (an unmodelled cell aborts the run; each run "
                        "reconciles its layer counts against yosys `stat`)",
         "measured": f"{sum(1 for b in blocks if b['status'] != 'ok')} aborted runs, "
                     f"{sum(len(b['errors']) for b in blocks)} errors",
         "ok": all(b["status"] == "ok" and not b["errors"] for b in blocks)},
        {"id": "equivalence", "requirement": "the Boolean graph and the ASAP7 netlist are proven to match the RTL",
         "measured": f"{eq_status.count('proven')} proven, {eq_status.count('bounded')} bounded, "
                     f"{eq_status.count('failed')} failed, {unproven} unproven pairs",
         "ok": unproven == 0 and not any(s in (None, "failed") for s in eq_status)},
        {"id": "simulation", "requirement": "random RTL vs netlist simulation finds no mismatch",
         "measured": f"{len(sims)} blocks simulated, {sum(s['compared_bits'] or 0 for s in sims)} bits compared, "
                     f"{mismatches} mismatches", "ok": mismatches == 0},
        {"id": "provenance", "requirement": "report nodes link back to RTL source locations where Yosys has them",
         "measured": "`src` on " + ", ".join(f"{pct(*src[layer])} {layer}" for layer in src),
         "ok": src_ok},
        {"id": "determinism", "requirement": "the same sources and profile reproduce byte-identical layer files",
         "measured": det["measured"], "ok": det["ok"]},
    ]


def build(results: list[dict], baseline: dict | None = None) -> dict:
    blocks = [block_evidence(r) for r in results]
    crit = criteria(blocks, baseline)
    return {"schema_version": EVIDENCE_SCHEMA_VERSION,
            "criteria_passed": sum(c["ok"] is True for c in crit),
            "criteria_total": len(crit),
            "criteria_unknown": sum(c["ok"] is None for c in crit),
            "criteria": crit, "blocks": blocks}


def render(evidence: dict) -> str:
    mark = {True: "PASS", False: "FAIL", None: "?"}
    lines = ["# Suite evidence", "",
             f"{evidence['criteria_passed']}/{evidence['criteria_total']} criteria pass"
             + (f", {evidence['criteria_unknown']} not checked" if evidence["criteria_unknown"] else ""), "",
             "| | criterion | measured |", "|---|---|---|"]
    lines += [f"| {mark[c['ok']]} | {c['requirement']} | {c['measured']} |" for c in evidence["criteria"]]
    lines += ["", "## Blocks", "", "| block | layers | cells | area (um2) | equivalence | simulation |",
              "|---|---|---|---|---|---|"]
    for b in evidence["blocks"]:
        sim = b["simulation"]
        sim_txt = (f"{sim['mismatches']} mismatches / {sim['compared_bits']} bits" if sim["status"] else "-")
        eq = b["equivalence"]
        eq_txt = eq["status"] or "-"
        if eq["checks"]:
            eq_txt += " (" + ", ".join(f"{c['proven']}/{(c['proven'] or 0) + (c['unproven'] or 0)}"
                                       for c in eq["checks"].values()) + ")"
        lines.append(f"| {b['name']} | {'4/4' if b['layers_present'] else 'MISSING'} | {b['mapped']['cells']} | "
                     f"{b['mapped']['area']} | {eq_txt} | {sim_txt} |")
    return "\n".join(lines) + "\n"


def write(results: list[dict], out_dir: Path, baseline_path: Path | None = None) -> dict:
    baseline = json.loads(baseline_path.read_text()) if baseline_path else None
    evidence = build(results, baseline)
    (out_dir / "suite_evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (out_dir / "EVIDENCE.md").write_text(render(evidence))
    return evidence


if __name__ == "__main__":  # pragma: no cover - re-render from an existing suite run
    import sys
    summary = json.loads(Path(sys.argv[1]).read_text())
    base = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    ev = write(summary["results"], Path(sys.argv[1]).resolve().parent, base)
    sys.stdout.write(render(ev))
