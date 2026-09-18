#!/usr/bin/env python3
"""
mapped_cells: describe the technology-mapped netlist (mapped_yosys.json) cell by cell as
mapped_cells.json — Liberty cell type, its area and logic function, and the net on every pin
with its direction — plus per-type totals.

    mapped_cells.py out/mapped_yosys.json --top fifo --liberty a.lib.gz b.lib.gz -o out/mapped_cells.json

Area, pin directions and functions come from the same pinned Liberty files the mapping used,
read with a small scanner (cell name, `area`, `pin` direction/function); Yosys' `write_json`
carries only the connections. Nets are named like the other layers: a kept RTL wire keeps its
name (`count_q[3]`), an ABC-internal net keeps its Yosys name. A cell type missing from the
libraries is an error, not a zero. Cells are ordered by (type, output net, Yosys cell) and
numbered `cellNNNN`, so identical inputs give identical files.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from pathlib import Path

from boolean_graph import UnsupportedCell, pick_module
from word_level import bit_names, normalize_src, render_signal

MAPPED_SCHEMA_VERSION = 1

CELL_RE = re.compile(r'^\s*cell\s*\(\s*"?([^\s")]+)"?\s*\)\s*\{', re.MULTILINE)
PIN_RE = re.compile(r'^\s*(pg_pin|pin|bus)\s*\(\s*"?([^\s")]+)"?\s*\)\s*\{', re.MULTILINE)
ATTR_RE = re.compile(r'\b(area|direction|function)\s*:\s*"?([^;"\n]*)"?\s*;?')


def read_liberty_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def parse_liberty(text: str, only: set[str] | None = None) -> dict[str, dict]:
    """cell name -> {"area": float | None, "pins": {pin: {"direction": str, "function": str | None}}}.
    A flat scan: each `cell (...) {` group runs to the next one; `pin`/`bus` groups inside it own the
    `direction`/`function` attributes that follow them until the next pin (power pins are skipped).
    `only` restricts the (slow) body scan to the cell types a netlist actually uses."""
    cells: dict[str, dict] = {}
    heads = list(CELL_RE.finditer(text))
    for i, head in enumerate(heads):
        if only is not None and head.group(1) not in only:
            continue
        body = text[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        cell = {"area": None, "pins": {}}
        pin_heads = list(PIN_RE.finditer(body))
        cell_attrs = body[: pin_heads[0].start()] if pin_heads else body
        for m in ATTR_RE.finditer(cell_attrs):
            if m.group(1) == "area":
                cell["area"] = float(m.group(2))
        for j, ph in enumerate(pin_heads):
            seg = body[ph.end(): pin_heads[j + 1].start() if j + 1 < len(pin_heads) else len(body)]
            if ph.group(1) == "pg_pin":
                continue
            pin = {"direction": "unknown", "function": None}
            for m in ATTR_RE.finditer(seg):
                if m.group(1) == "direction":
                    pin["direction"] = m.group(2).strip()
                elif m.group(1) == "function":
                    pin["function"] = m.group(2).strip()
            cell["pins"][ph.group(2)] = pin
        cells[head.group(1)] = cell
    return cells


def load_liberty(paths: list[Path], only: set[str] | None = None) -> dict[str, dict]:
    lib: dict[str, dict] = {}
    for p in paths:
        lib.update(parse_liberty(read_liberty_text(p), only))
    return lib


def cell_types(data: dict, top: str | None) -> set[str]:
    return {c["type"] for c in pick_module(data, top)[1].get("cells", {}).values()}


def build_cells(data: dict, top: str | None, lib: dict[str, dict], src_base: Path | None = None) -> dict:
    """`lib` is `load_liberty(...)`; it must cover every cell type of the module or the report fails."""
    top_name, mod = pick_module(data, top)
    names = bit_names(mod)
    missing: Counter[str] = Counter()
    cells: list[dict] = []
    for cname, c in mod.get("cells", {}).items():
        ctype = c["type"]
        spec = lib.get(ctype)
        if spec is None or spec["area"] is None:
            missing[ctype] += 1
            continue
        pins = {}
        outputs = []
        for pin, bits in sorted(c.get("connections", {}).items()):
            pspec = spec["pins"].get(pin, {"direction": "unknown", "function": None})
            pins[pin] = {"direction": pspec["direction"], "net": render_signal(bits, names)}
            if pspec["function"] is not None:
                pins[pin]["function"] = pspec["function"]
            if pspec["direction"] == "output":
                outputs.append(pins[pin]["net"])
        cells.append({
            "id": None,
            "type": ctype,
            "yosys_cell": cname,
            "area": spec["area"],
            "pins": pins,
            "src": normalize_src(c.get("attributes", {}).get("src"), src_base),
            "_out": outputs[0] if outputs else "",
        })
    if missing:
        listing = ", ".join(f"{t} x{n}" for t, n in sorted(missing.items()))
        raise UnsupportedCell(f"mapped cell types without a Liberty area: {listing}")

    cells.sort(key=lambda c: (c["type"], c["_out"], c["yosys_cell"]))
    for i, c in enumerate(cells):
        c["id"] = f"cell{i:04d}"
        del c["_out"]

    by_type: dict[str, dict] = {}
    for c in cells:
        g = by_type.setdefault(c["type"], {"count": 0, "area_each": c["area"], "area": 0.0})
        g["count"] += 1
        g["area"] = round(g["area"] + c["area"], 6)
    ports = {n: {"direction": p["direction"], "width": len(p["bits"])} for n, p in mod.get("ports", {}).items()}
    return {
        "schema_version": MAPPED_SCHEMA_VERSION,
        "top": top_name,
        "ports": ports,
        "cells": cells,
        "summary": {
            "cells": len(cells),
            "area": round(sum(c["area"] for c in cells), 6),
            "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1]["count"], kv[0]))),
            "pins": sum(len(c["pins"]) for c in cells),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("yosys_json")
    ap.add_argument("--top")
    ap.add_argument("--liberty", nargs="+", type=Path, required=True)
    ap.add_argument("-o", "--out", default="mapped_cells.json")
    ap.add_argument("--src-base", type=Path)
    args = ap.parse_args(argv)
    try:
        data = json.loads(Path(args.yosys_json).read_text())
        report = build_cells(data, args.top, load_liberty(args.liberty, cell_types(data, args.top)), args.src_base)
        Path(args.out).write_text(json.dumps(report, indent=1) + "\n")
    except (OSError, ValueError, KeyError, UnsupportedCell) as e:
        print(f"[mapped_cells] error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
