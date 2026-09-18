"""
run_report: the two run-level artifacts bool_area.py writes last, built purely from the metrics
dict so they need no re-parsing of the layer files.

* run_manifest.json — provenance: what produced this directory (tool, argv, yosys/sv2v versions),
  from which inputs (sources + sha256, profile + sha256, pinned Liberty sha256s), and which files
  it left behind (path, size, sha256, layer schema version). Two runs with identical inputs and
  tools give identical manifests apart from `argv`/paths.
* summary.md — a one-page human digest of the same numbers metrics.json carries, layer by layer.
"""

from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1

# layer name -> artifact key in bool_area.ARTIFACTS
LAYERS = {"word_level": "word", "sequential_overlay": "sequential", "boolean_graph": "graph",
          "mapped_cells": "mapped_cells"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(metrics: dict, out: dict[str, Path], argv: list[str], schema_versions: dict[str, int]) -> dict:
    """`out` maps artifact keys to paths (bool_area.ARTIFACTS); `schema_versions` maps layer name
    (`word_level`, `sequential_overlay`, `boolean_graph`, `mapped_cells`, `metrics`) to its version."""
    artifacts: dict[str, dict] = {}
    for key, path in sorted(out.items()):
        if key == "manifest":
            continue
        entry: dict = {"path": str(path), "exists": path.is_file()}
        if entry["exists"]:
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = sha256_file(path)
        artifacts[key] = entry
    layers = {layer: {"file": out[art].name, "schema_version": schema_versions[layer], "present": artifacts[art]["exists"]}
              for layer, art in LAYERS.items()}
    layers["metrics"] = {"file": out["metrics"].name, "schema_version": schema_versions["metrics"], "present": True}
    tools = metrics.get("tools") or {}
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "top": metrics["top"],
        "status": metrics["status"],
        "stage": metrics["stage"],
        "generated_by": {
            "tool": "bool_area.py",
            "argv": list(argv),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "yosys": tools.get("yosys"),
            "yosys_version": tools.get("yosys_version"),
            "sv2v": tools.get("sv2v"),
            "sv2v_version": tools.get("sv2v_version"),
            "iverilog": tools.get("iverilog"),
            "vvp": tools.get("vvp"),
        },
        "sources": metrics["sources"],
        "profile": metrics["profile"],
        "frontend": metrics["frontend"],
        "layers": layers,
        "artifacts": artifacts,
        "timing": metrics.get("timing", {}),
        "errors": metrics.get("errors", []),
    }


def counts(d: dict | None, limit: int | None = None) -> str:
    items = [(k, v) for k, v in (d or {}).items() if v]
    if limit is not None and len(items) > limit:
        items = items[:limit] + [(f"+{len(items) - limit} more", "")]
    return ", ".join(f"{k} {v}" if v != "" else k for k, v in items) or "none"


def render_summary(metrics: dict) -> str:
    m = metrics
    lines = [f"# {m['top']} — design-layer summary", ""]
    prof = m.get("profile") or {}
    tools = m.get("tools") or {}
    fe = m.get("frontend") or {}
    lines.append(f"- status: **{m['status']}** (stage `{m['stage']}`), wall {m.get('wall_seconds')} s")
    if prof:
        lines.append(f"- profile: `{prof['name']}` v{prof['version']} (`{prof['sha256'][:12]}`), "
                     f"{len(prof.get('liberty', []))} pinned Liberty files"
                     f"{'' if prof.get('liberty_verified') else ' (sha256 NOT verified)'}")
    if fe:
        lines.append(f"- frontend: `{fe['name']}`; yosys: {tools.get('yosys_version', '?')}")
    lines += ["", "## Sources", ""]
    for s in m.get("sources", []):
        lines.append(f"- `{s['path']}` sha256 `{(s.get('sha256') or '?')[:12]}`")

    w = m.get("word_level")
    if w:
        lines += ["", "## Word level (`word_level.json`)", "",
                  f"{w['operations']} operations: {counts(w.get('by_kind'))}",
                  (f"register bits {w['register_bits']}, memory bits {w['memory_bits']}, "
                   f"input bits {w['input_bits']}, output bits {w['output_bits']}")]
    s = m.get("sequential")
    if s:
        clocks = "; ".join(f"`{c['signal']}` {c['edge']} ({c['bits']} bits)" for c in s.get("clocks", [])) or "none"
        resets = "; ".join(f"`{r['signal']}` {r['kind']} active-{r['active']} ({r['bits']} bits)"
                           for r in s.get("resets", [])) or "none"
        lines += ["", "## Sequential (`sequential_overlay.json`)", "",
                  f"{s['registers']} registers / {s['register_bits']} bits: {counts(s.get('by_type'))}",
                  f"clocks: {clocks}", f"resets: {resets}",
                  f"enable bits {s['enable_bits']}, init bits {s['init_bits']}"]
    b = m.get("boolean")
    if b:
        lines += ["", "## Boolean (`boolean_graph.json`)", "",
                  f"{b['gate_total']} gates ({counts(b.get('gates_by_type'))}), {b['dff']} DFF, {b['edge_total']} edges",
                  f"depth {b['max_depth']}, max fanout {b['max_fanout']}, avg fanout {b['avg_fanout']}"]
    mp = m.get("mapped")
    if mp:
        lines += ["", "## Mapped (`mapped_cells.json`)", "",
                  (f"{mp['num_cells']} cells ({mp['num_flops']} flops), area **{mp['area']} {mp.get('area_unit', '')}** "
                   f"(sequential {mp.get('sequential_area')})"), "", "| cell type | count |", "|---|---|"]
        lines += [f"| `{t}` | {n} |" for t, n in mp.get("cells_by_type", {}).items()]

    eq, sim = m.get("equivalence"), m.get("simulation")
    if eq or sim:
        lines += ["", "## Verification", ""]
    if eq:
        checks = "; ".join(f"{n}: {c['status']} ({c.get('proven')}/{c.get('equiv_cells')} pairs, {c.get('seconds')} s)"
                           for n, c in eq.get("checks", {}).items())
        lines.append(f"- equivalence **{eq['status']}** — {checks}")
    if sim:
        lines.append(f"- simulation **{sim['status']}** — {sim.get('cycles')} cycles, "
                     f"{sim.get('compared_bits')} bits compared, {sim.get('mismatches')} mismatches")

    if m.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {x}" for x in m["warnings"]]
    if m.get("errors"):
        lines += ["", "## Errors", ""] + [f"- {x}" for x in m["errors"]]
    lines += ["", "## Artifacts", ""] + [f"- `{Path(v).name}`" for v in sorted(m.get("artifacts", {}).values())]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - tiny convenience: render summary.md from a metrics.json
    import json
    sys.stdout.write(render_summary(json.loads(Path(sys.argv[1]).read_text())))
