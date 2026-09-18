#!/usr/bin/env python3
"""
sequential_overlay: describe every register of the word-level checkpoint (word_yosys.json) in
RTL terms — clock and edge, reset kind/polarity/value, enable and polarity, power-up init, the
RTL signal each Q and D bit belongs to, and the source line — as sequential_overlay.json.

    sequential_overlay.py out/word_yosys.json --top fifo -o out/sequential_overlay.json

This is the layer the Boolean graph's depth metric relies on: registers are where combinational
paths start and end, so this file makes those boundaries explicit and names them. Per-bit `q`
labels use the same `name[idx]` convention as boolean_graph.json DFF nodes, so the two files
join on that label.

Only RTLIL register cells are accepted (`$dff`, `$adff`, `$sdff`, `$aldff`, `$dffsr` and their
enable variants); memories, latches and everything combinational are not this layer's job and
are skipped by type, not by guessing. Output is deterministic: registers are ordered by
(Q signal, Yosys cell) and given ids `regNNNN`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from boolean_graph import UnsupportedCell, pick_module
from word_level import BitName, bit_names, normalize_src, param_value, render_signal

SEQ_SCHEMA_VERSION = 1

REGISTER_TYPES = frozenset({
    "$dff", "$dffe", "$adff", "$adffe", "$sdff", "$sdffe", "$sdffce", "$aldff", "$aldffe", "$dffsr", "$dffsre",
})


def polarity(params: dict, key: str) -> str:
    return "high" if params.get(key, 1) else "low"


def const_text(value: int | str, width: int) -> str:
    """`ARST_VALUE`-style parameter as Verilog text: ints as zero-padded binary, x/z strings verbatim."""
    bits = format(value, f"0{width}b") if isinstance(value, int) else value
    return f"{width}'b{bits}"


def bit_label(b: int | str, names: dict[int, BitName]) -> str:
    """One bit as RTL text, with boolean_graph.bit_label's rule that a bare 1-bit wire is just its name."""
    if isinstance(b, str):
        return f"1'b{b}"
    if b not in names:
        return f"$bit{b}"
    wname, idx, offset, width = names[b]
    return wname if width == 1 and offset == 0 else f"{wname}[{idx}]"


def init_bits(mod: dict) -> dict[int, str]:
    """bit -> power-up value ('0'/'1') from wire `init` attributes; undefined (x) bits are absent."""
    out: dict[int, str] = {}
    for w in mod.get("netnames", {}).values():
        init = w.get("attributes", {}).get("init")
        if init is None:
            continue
        width = len(w["bits"])
        text = format(init, f"0{width}b") if isinstance(init, int) else init.rjust(width, "x")
        for b, v in zip(w["bits"], reversed(text)):
            if isinstance(b, int) and v in "01":
                out.setdefault(b, v)
    return out


def describe_reset(ctype: str, params: dict, sig: dict[str, str], width: int) -> dict | None:
    if "ARST" in sig:
        return {"kind": "async", "signal": sig["ARST"], "active": polarity(params, "ARST_POLARITY"),
                "value": const_text(params["ARST_VALUE"], width)}
    if "SRST" in sig:
        return {"kind": "sync", "signal": sig["SRST"], "active": polarity(params, "SRST_POLARITY"),
                "value": const_text(params["SRST_VALUE"], width), "gated_by_enable": ctype == "$sdffce"}
    if "ALOAD" in sig:
        return {"kind": "async_load", "signal": sig["ALOAD"], "active": polarity(params, "ALOAD_POLARITY"),
                "value": sig["AD"]}
    if "SET" in sig:
        return {"kind": "async_set_clear",
                "set": {"signal": sig["SET"], "active": polarity(params, "SET_POLARITY")},
                "clear": {"signal": sig["CLR"], "active": polarity(params, "CLR_POLARITY")}}
    return None


def build_overlay(data: dict, top: str | None, src_base: Path | None = None) -> dict:
    top_name, mod = pick_module(data, top)
    names = bit_names(mod)
    inits = init_bits(mod)
    regs: list[dict] = []
    for cname, c in mod.get("cells", {}).items():
        ctype = c["type"]
        if ctype not in REGISTER_TYPES:
            continue
        params = {k: param_value(v) for k, v in c.get("parameters", {}).items()}
        conns = c.get("connections", {})
        sig = {pin: render_signal(bits, names) for pin, bits in conns.items()}
        q_bits, d_bits = conns["Q"], conns["D"]
        width = len(q_bits)
        if len(d_bits) != width:
            raise ValueError(f"register {cname}: D width {len(d_bits)} != Q width {width}")
        init = "".join(inits.get(b, "x") if isinstance(b, int) else "x" for b in reversed(q_bits))
        regs.append({
            "id": None,
            "width": width,
            "yosys_type": ctype,
            "yosys_cell": cname,
            "clock": {"signal": sig["CLK"], "edge": "posedge" if params.get("CLK_POLARITY", 1) else "negedge"},
            "reset": describe_reset(ctype, params, sig, width),
            "enable": {"signal": sig["EN"], "active": polarity(params, "EN_POLARITY")} if "EN" in sig else None,
            "init": None if set(init) == {"x"} else f"{width}'b{init}",
            "d": sig["D"],
            "q": sig["Q"],
            "bits": [
                {"q": bit_label(qb, names), "d": bit_label(db, names), "init": inits.get(qb) if isinstance(qb, int) else None}
                for qb, db in zip(q_bits, d_bits)
            ],
            "src": normalize_src(c.get("attributes", {}).get("src"), src_base),
        })
    regs.sort(key=lambda r: (r["q"], r["yosys_cell"]))
    for i, r in enumerate(regs):
        r["id"] = f"reg{i:04d}"

    def tally(groups: dict[tuple, dict], key: tuple, fields: dict, width: int) -> None:
        g = groups.setdefault(key, {**fields, "registers": 0, "bits": 0})
        g["registers"] += 1
        g["bits"] += width

    clocks: dict[tuple, dict] = {}
    resets: dict[tuple, dict] = {}
    for r in regs:
        clk = r["clock"]
        tally(clocks, (clk["signal"], clk["edge"]), clk, r["width"])
        rs = r["reset"]
        if rs and "signal" in rs:
            fields = {"signal": rs["signal"], "kind": rs["kind"], "active": rs["active"]}
            tally(resets, tuple(fields.values()), fields, r["width"])
    kinds = Counter((r["reset"] or {}).get("kind", "none") for r in regs)
    return {
        "schema_version": SEQ_SCHEMA_VERSION,
        "top": top_name,
        "registers": regs,
        "summary": {
            "registers": len(regs),
            "register_bits": sum(r["width"] for r in regs),
            "by_type": dict(sorted(Counter(r["yosys_type"] for r in regs).items())),
            "clocks": [clocks[k] for k in sorted(clocks)],
            "resets": [resets[k] for k in sorted(resets)],
            "reset_bits": {k: sum(r["width"] for r in regs if (r["reset"] or {}).get("kind", "none") == k)
                           for k in sorted(kinds)},
            "enable_bits": sum(r["width"] for r in regs if r["enable"]),
            "init_bits": sum(b["init"] is not None for r in regs for b in r["bits"]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("yosys_json", help="write_json output taken after proc/flatten/opt_dff (word_yosys.json)")
    ap.add_argument("--top", help="module to report (default: the only / top-attributed module)")
    ap.add_argument("-o", "--out", default="sequential_overlay.json")
    ap.add_argument("--src-base", type=Path, help="directory yosys ran in (anchors relative src paths)")
    args = ap.parse_args(argv)
    try:
        report = build_overlay(json.loads(Path(args.yosys_json).read_text()), args.top, args.src_base)
        Path(args.out).write_text(json.dumps(report, indent=1) + "\n")
    except (OSError, ValueError, KeyError, UnsupportedCell) as e:
        print(f"[sequential_overlay] error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
