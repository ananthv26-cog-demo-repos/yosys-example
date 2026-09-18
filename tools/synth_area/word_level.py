#!/usr/bin/env python3
"""
word_level: import a Yosys `write_json` dump taken after elaboration + process lowering
(`proc; flatten; opt_dff`) and *before* any bit-level mapping into a word-level report.

    word_level.py word_yosys.json --top fifo -o word_level.json

At this stage the design is still made of multi-bit RTLIL cells: arithmetic (`$add`),
comparisons (`$eq`, `$lt`), bitwise/reduction/logical operators, shifts, muxes (`$mux`,
`$pmux`), registers (`$adffe`, ...) and memory ports (`$memrd_v2`, `$memwr_v2`). Each cell
becomes one `operation` with its kind, result width, per-operand signedness and width, the
signals on every pin (rendered like RTL: `count_q[2:0]`, `{a[1:0], b}`, `3'b001`) and the
`src` location Yosys carried from the frontend. Slices and concatenations are not cells in
RTLIL, so they appear as the pin expressions themselves.

Every cell type outside `KINDS` is an error: nothing is silently dropped or approximated.
Operation ids are project-owned (`op0000`, ...) and assigned in a deterministic order
(kind, output signal, Yosys name), so two runs of the same input produce identical files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from boolean_graph import UnsupportedCell, pick_module

WORD_SCHEMA_VERSION = 1

# RTLIL cell type -> (report kind, category). Registers are listed so a register-containing
# design is accepted here; their detailed description is the sequential overlay's job.
KINDS: dict[str, tuple[str, str]] = {
    "$add": ("ADD", "arithmetic"), "$sub": ("SUB", "arithmetic"), "$mul": ("MUL", "arithmetic"),
    "$div": ("DIV", "arithmetic"), "$mod": ("MOD", "arithmetic"), "$divfloor": ("DIVFLOOR", "arithmetic"),
    "$modfloor": ("MODFLOOR", "arithmetic"), "$pow": ("POW", "arithmetic"), "$neg": ("NEG", "arithmetic"),
    "$pos": ("POS", "arithmetic"),
    "$eq": ("EQ", "compare"), "$ne": ("NE", "compare"), "$eqx": ("EQX", "compare"), "$nex": ("NEX", "compare"),
    "$lt": ("LT", "compare"), "$le": ("LE", "compare"), "$gt": ("GT", "compare"), "$ge": ("GE", "compare"),
    "$not": ("NOT", "bitwise"), "$and": ("AND", "bitwise"), "$or": ("OR", "bitwise"),
    "$xor": ("XOR", "bitwise"), "$xnor": ("XNOR", "bitwise"),
    "$reduce_and": ("REDUCE_AND", "reduce"), "$reduce_or": ("REDUCE_OR", "reduce"),
    "$reduce_xor": ("REDUCE_XOR", "reduce"), "$reduce_xnor": ("REDUCE_XNOR", "reduce"),
    "$reduce_bool": ("REDUCE_BOOL", "reduce"),
    "$logic_not": ("LOGIC_NOT", "logical"), "$logic_and": ("LOGIC_AND", "logical"), "$logic_or": ("LOGIC_OR", "logical"),
    "$shl": ("SHL", "shift"), "$shr": ("SHR", "shift"), "$sshl": ("SSHL", "shift"), "$sshr": ("SSHR", "shift"),
    "$shift": ("SHIFT", "shift"), "$shiftx": ("SHIFTX", "shift"),
    "$mux": ("MUX", "mux"), "$pmux": ("PMUX", "mux"), "$bmux": ("BMUX", "mux"), "$demux": ("DEMUX", "mux"),
    "$memrd": ("MEMRD", "memory"), "$memrd_v2": ("MEMRD", "memory"),
    "$memwr": ("MEMWR", "memory"), "$memwr_v2": ("MEMWR", "memory"),
    "$meminit": ("MEMINIT", "memory"), "$meminit_v2": ("MEMINIT", "memory"),
    "$dff": ("REG", "register"), "$dffe": ("REG", "register"), "$adff": ("REG", "register"),
    "$adffe": ("REG", "register"), "$sdff": ("REG", "register"), "$sdffe": ("REG", "register"),
    "$sdffce": ("REG", "register"), "$aldff": ("REG", "register"), "$aldffe": ("REG", "register"),
    "$dffsr": ("REG", "register"), "$dffsre": ("REG", "register"),
}


def output_pins(kind: str, conns: dict, dirs: dict) -> list[str]:
    """write_json normally carries `port_directions`; without them fall back to the RTLIL convention
    (result on Y/Q, and DATA is only a result on a memory *read* port)."""
    if dirs:
        return [p for p in conns if dirs.get(p) == "output"]
    return [p for p in conns if p in ("Y", "Q") or (p == "DATA" and kind == "MEMRD")]


def param_value(raw: int | str) -> int | str:
    """Decode a write_json parameter: ints stay ints, all-binary strings become ints, strings with
    x/z bits (e.g. an undefined ARST_VALUE) are kept verbatim. write_json marks text parameters
    that happen to look like bit vectors with a trailing space, which is stripped here."""
    if isinstance(raw, int):
        return raw
    if raw.endswith(" "):
        return raw.removesuffix(" ")
    if raw and all(ch in "01" for ch in raw):
        return int(raw, 2)
    return raw


def normalize_src(src: str | None, base: Path | None) -> str | None:
    """Yosys `src` attributes (`file:line.col-line.col`, several joined by `|`) keep the path as the
    frontend saw it, relative to the yosys working directory; anchor relative ones on `base`."""
    if not src:
        return None
    if base is None:
        return src
    locs = []
    for loc in src.split("|"):
        path, sep, span = loc.rpartition(":")
        if sep and path and not os.path.isabs(path):
            path = os.path.normpath(base / path)
        locs.append(f"{path}{sep}{span}")
    return "|".join(locs)


def memory_name(memid: int | str | None) -> str | None:
    """`MEMID` parameters are RTLIL ids (`\\u_fifo.mem`); the module's `memories` table uses the
    public form without the backslash."""
    if not isinstance(memid, str):
        return None
    return memid.removeprefix("\\")


BitName = tuple[str, int, int, int]  # (wire, HDL index, wire offset, wire width)


def bit_names(mod: dict) -> dict[int, BitName]:
    """bit -> preferred wire of every bit. Yosys keeps every alias of a net (`count_o`,
    `u_fifo.count_o`, `u_fifo.count_q`): public names beat Yosys-internal (`hide_name`) ones,
    then the shallowest, shortest name wins so top-level ports beat hierarchical internals."""
    ranked = sorted(
        mod.get("netnames", {}).items(),
        key=lambda kv: (kv[1].get("hide_name", 0), kv[0].count("."), len(kv[0]), kv[0]),
    )
    names: dict[int, BitName] = {}
    for wname, w in ranked:
        width = len(w.get("bits", []))
        offset = w.get("offset", 0)
        for i, b in enumerate(w["bits"]):
            if isinstance(b, int) and b not in names:
                names[b] = (wname, offset + (width - 1 - i if w.get("upto", 0) else i), offset, width)
    return names


def render_signal(bits: list[int | str], names: dict[int, BitName]) -> str:
    """RTL-style text for a pin's bit vector: a whole wire is its name, runs of one wire collapse
    to `name[hi:lo]`, constant runs to `N'bxxx`, and mixed vectors to a `{...}` concatenation
    (MSB first, like Verilog)."""
    segments: list[str] = []
    i = 0
    while i < len(bits):
        b = bits[i]
        if isinstance(b, str):
            j = i
            while j < len(bits) and isinstance(bits[j], str):
                j += 1
            value = "".join(str(x) for x in reversed(bits[i:j]))
            segments.append(f"{j - i}'b{value}")
            i = j
            continue
        if b not in names:
            segments.append(f"$bit{b}")
            i += 1
            continue
        wname, idx, offset, width = names[b]
        # a run of the same wire with consecutive indices (descending for an `upto` wire)
        j = i + 1
        step = 1
        if j < len(bits) and names.get(bits[j]) == (wname, idx - 1, offset, width):
            step = -1
        while j < len(bits) and names.get(bits[j]) == (wname, idx + step * (j - i), offset, width):
            j += 1
        first, last = idx, idx + step * (j - i - 1)
        if {first, last} == {offset, offset + width - 1}:
            segments.append(wname)
        elif first == last:
            segments.append(f"{wname}[{first}]")
        else:
            segments.append(f"{wname}[{last}:{first}]")
        i = j
    if len(segments) == 1:
        return segments[0]
    return "{" + ", ".join(reversed(segments)) + "}"


def build_report(data: dict, top: str | None, src_base: Path | None = None) -> dict:
    """`src_base` is the directory yosys ran in; relative `src` paths are anchored on it."""
    top_name, mod = pick_module(data, top)
    names = bit_names(mod)
    unsupported: Counter[str] = Counter()
    ops: list[dict] = []
    for cname, c in mod.get("cells", {}).items():
        ctype = c["type"]
        if ctype not in KINDS:
            unsupported[ctype] += 1
            continue
        kind, category = KINDS[ctype]
        params = {k: param_value(v) for k, v in sorted(c.get("parameters", {}).items())}
        conns = dict(sorted(c.get("connections", {}).items()))
        dirs = c.get("port_directions", {})
        pins = {pin: {"width": len(bits), "signal": render_signal(bits, names)} for pin, bits in conns.items()}
        outputs = output_pins(kind, conns, dirs)
        inputs = [p for p in conns if p not in outputs]
        width = pins[outputs[0]]["width"] if outputs else params.get("WIDTH")
        signed = {p: bool(params[f"{p}_SIGNED"]) for p in ("A", "B") if f"{p}_SIGNED" in params}
        op = {
            "id": None,
            "kind": kind,
            "category": category,
            "yosys_type": ctype,
            "yosys_cell": cname,
            "width": width,
            "signed": signed,
            "inputs": {p: pins[p] for p in inputs},
            "outputs": {p: pins[p] for p in outputs},
            "parameters": params,
            "src": normalize_src(c.get("attributes", {}).get("src"), src_base),
        }
        ops.append(op)
    if unsupported:
        listing = ", ".join(f"{t} x{n}" for t, n in sorted(unsupported.items()))
        raise UnsupportedCell(f"unsupported cell types in word-level layer: {listing}")

    def sort_key(op: dict) -> tuple:
        out = next(iter(op["outputs"].values()), {"signal": ""})["signal"]
        return (op["kind"], out, op["yosys_cell"])

    ops.sort(key=sort_key)
    for i, op in enumerate(ops):
        op["id"] = f"op{i:04d}"

    memories = [
        {
            "name": mname,
            "width": m["width"],
            "size": m["size"],
            "start_offset": m.get("start_offset", 0),
            "bits": m["width"] * m["size"],
            "read_ports": sum(1 for op in ops if op["kind"] == "MEMRD" and memory_name(op["parameters"].get("MEMID")) == mname),
            "write_ports": sum(1 for op in ops if op["kind"] == "MEMWR" and memory_name(op["parameters"].get("MEMID")) == mname),
            "src": normalize_src(m.get("attributes", {}).get("src"), src_base),
        }
        for mname, m in sorted(mod.get("memories", {}).items())
    ]
    ports = {
        n: {"direction": p["direction"], "width": len(p["bits"])}
        for n, p in mod.get("ports", {}).items()
    }
    counts = Counter(op["kind"] for op in ops)
    return {
        "schema_version": WORD_SCHEMA_VERSION,
        "top": top_name,
        "src": normalize_src(mod.get("attributes", {}).get("src"), src_base),
        "ports": ports,
        "operations": ops,
        "memories": memories,
        "summary": {
            "operations": len(ops),
            "by_kind": dict(sorted(counts.items())),
            "by_category": dict(sorted(Counter(op["category"] for op in ops).items())),
            "register_bits": sum(op["width"] for op in ops if op["kind"] == "REG"),
            "memory_bits": sum(m["bits"] for m in memories),
            "input_bits": sum(p["width"] for p in ports.values() if p["direction"] in ("input", "inout")),
            "output_bits": sum(p["width"] for p in ports.values() if p["direction"] in ("output", "inout")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("yosys_json", help="output of `write_json` after `proc; flatten; opt_dff`")
    ap.add_argument("--top", help="module to import (default: the only module)")
    ap.add_argument("-o", "--out", default="word_level.json")
    ap.add_argument("--src-base", type=Path, help="directory yosys ran in (anchors relative src paths)")
    args = ap.parse_args(argv)
    try:
        report = build_report(json.loads(Path(args.yosys_json).read_text()), args.top, args.src_base)
        Path(args.out).write_text(json.dumps(report, indent=1) + "\n")
    except (OSError, ValueError, KeyError) as e:
        print(f"[word_level] error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
