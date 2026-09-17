#!/usr/bin/env python3
"""
boolean_graph: import a Yosys `write_json` netlist of one-bit gates into an explicit
Boolean graph (nodes + edges) and compute structural metrics.

    boolean_graph.py generic_yosys.json --top fifo -o boolean_graph.json [--metrics metrics.json]
                     [--profile asap7_rvt_tt_v1 | --dff-type '$_DFF_P_' ...]

Node kinds: INPUT / OUTPUT (one per port bit), CONST0 / CONST1 / CONSTX, the gates
NOT AND NAND OR NOR XOR XNOR MUX, and DFF (one-bit flop; `dff_type` keeps the exact
Yosys cell so reset polarity etc. is not lost). Every other cell type is an error:
the caller asked for a graph of supported gates only, so nothing is approximated. The
DFF types accepted are the profile's `dff_types` list (or an explicit `--dff-type` list).

Port and wire bits are named with their HDL index (`data[4]` for the low bit of
`[7:4] data`), honouring Yosys' `offset` / `upto` vector metadata.

Edges are (driver node -> sink node, sink pin). `max_depth` is the longest chain of
combinational gates from a graph source (input, constant, DFF Q) to a data sink (output
port or DFF D pin); logic feeding clock or asynchronous set/reset pins is not counted.
`max_fanout` / `avg_fanout` count data sink pins per driving node; DFF clock pins and
asynchronous set/reset pins are reported separately (`clock_fanout`, `control_fanout`).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

GRAPH_SCHEMA_VERSION = 1
HERE = Path(__file__).resolve().parent
PROFILES_DIR = HERE / "profiles"
DEFAULT_PROFILE = "asap7_rvt_tt_v1"

GATE_TYPES: dict[str, str] = {
    "$_NOT_": "NOT",
    "$_AND_": "AND",
    "$_NAND_": "NAND",
    "$_OR_": "OR",
    "$_NOR_": "NOR",
    "$_XOR_": "XOR",
    "$_XNOR_": "XNOR",
    "$_MUX_": "MUX",
}
GATE_INPUT_PINS: dict[str, tuple[str, ...]] = {
    "NOT": ("A",),
    "AND": ("A", "B"),
    "NAND": ("A", "B"),
    "OR": ("A", "B"),
    "NOR": ("A", "B"),
    "XOR": ("A", "B"),
    "XNOR": ("A", "B"),
    "MUX": ("A", "B", "S"),
}
DFF_PREFIX = "$_DFF_"
DFF_OUTPUT_PIN = "Q"
DFF_DATA_PIN = "D"
DFF_CLOCK_PIN = "C"
CONST_KIND = {"0": "CONST0", "1": "CONST1", "x": "CONSTX", "z": "CONSTX"}


class UnsupportedCell(ValueError):
    pass


def pick_module(data: dict, top: str | None) -> tuple[str, dict]:
    mods = data.get("modules", {})
    if not mods:
        raise ValueError("no modules in yosys json")
    if top:
        for name in (top, f"\\{top}"):
            if name in mods:
                return top, mods[name]
        raise ValueError(f"module {top!r} not in yosys json (have {sorted(mods)})")
    if len(mods) != 1:
        raise ValueError(f"json has {len(mods)} modules; pass --top")
    (name, mod), = mods.items()
    return name.lstrip("\\"), mod


def hdl_index(vec: dict, pos: int) -> int:
    """HDL bit index of list position `pos` in a write_json port/netname vector.

    `offset` is the lowest declared index; `upto` marks an ascending `[lo:hi]` declaration,
    whose first list entry is the *highest* index (Yosys lists bits LSB-first either way).
    """
    width = len(vec["bits"])
    offset = vec.get("offset", 0)
    return offset + (width - 1 - pos if vec.get("upto", 0) else pos)


def bit_label(name: str, vec: dict, pos: int) -> str:
    return name if len(vec["bits"]) == 1 and not vec.get("offset", 0) else f"{name}[{hdl_index(vec, pos)}]"


def load_profile_dff_types(name_or_path: str) -> set[str]:
    """`dff_types` allowlist of a synthesis profile (a name under profiles/ or a JSON path)."""
    p = Path(name_or_path)
    if not p.exists():
        p = PROFILES_DIR / f"{name_or_path}.json"
    try:
        profile = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"could not read profile {name_or_path!r}: {e}") from e
    types = profile.get("dff_types") if isinstance(profile, dict) else None
    if not isinstance(types, list) or not types or not all(isinstance(t, str) for t in types):
        raise ValueError(f"profile {p}: 'dff_types' must be a non-empty list of Yosys cell names")
    return set(types)


def build_graph(data: dict, top: str | None, dff_types: set[str] | None = None) -> dict:
    """Turn one module of a `write_json` dump into {"nodes": [...], "edges": [...], ...}.

    Yosys JSON identifies every one-bit signal by an integer "bit"; constants appear as the
    strings "0"/"1"/"x". A node per port bit / constant / cell gives one driver per bit.
    `dff_types` is the set of `$_DFF_*` cells to accept; `None` accepts every `$_DFF_*`
    variant (library use only — the CLI always passes a profile's list).
    """
    top_name, mod = pick_module(data, top)
    nodes: list[dict] = []
    driver_of: dict[int | str, int] = {}  # bit -> node id that drives it
    const_nodes: dict[str, int] = {}
    unsupported: Counter[str] = Counter()

    def new_node(kind: str, **kw) -> int:
        nid = len(nodes)
        nodes.append({"id": nid, "kind": kind, **kw})
        return nid

    def const_node(val: str) -> int:
        if val not in const_nodes:
            const_nodes[val] = new_node(CONST_KIND.get(val, "CONSTX"), value=val)
        return const_nodes[val]

    # names for bits (public wires first so a bit driven by a register keeps its RTL name)
    bit_name: dict[int, str] = {}
    for wname, w in sorted(mod.get("netnames", {}).items(), key=lambda kv: (kv[1].get("hide_name", 0), kv[0])):
        for i, b in enumerate(w.get("bits", [])):
            if isinstance(b, int) and b not in bit_name:
                bit_name[b] = bit_label(wname, w, i)

    ports = mod.get("ports", {})
    for pname, p in ports.items():
        for i, b in enumerate(p.get("bits", [])):
            label, idx = bit_label(pname, p, i), hdl_index(p, i)
            if p["direction"] in ("input", "inout"):
                nid = new_node("INPUT", name=label, port=pname, bit=idx)
                if isinstance(b, int):
                    driver_of.setdefault(b, nid)
            if p["direction"] in ("output", "inout"):
                new_node("OUTPUT", name=label, port=pname, bit=idx, source_bit=b)

    # cells: gates and flops
    cell_nodes: list[tuple[int, dict]] = []
    for cname, c in sorted(mod.get("cells", {}).items()):
        ctype = c["type"]
        conns = c.get("connections", {})
        src = c.get("attributes", {}).get("src") or None
        if ctype in GATE_TYPES:
            kind = GATE_TYPES[ctype]
            nid = new_node("GATE", type=kind, cell=cname, src=src)
            out_bits = conns.get("Y", [])
            for b in out_bits:
                if isinstance(b, int):
                    driver_of[b] = nid
            cell_nodes.append((nid, c))
        elif ctype.startswith(DFF_PREFIX) and (dff_types is None or ctype in dff_types):
            nid = new_node("DFF", type="DFF", dff_type=ctype, cell=cname, src=src)
            for b in conns.get(DFF_OUTPUT_PIN, []):
                if isinstance(b, int):
                    driver_of[b] = nid
            cell_nodes.append((nid, c))
        else:
            unsupported[ctype] += 1
    if unsupported:
        listing = ", ".join(f"{t} x{n}" for t, n in sorted(unsupported.items()))
        raise UnsupportedCell(f"unsupported cell types in Boolean layer: {listing}")

    def driver(b: int | str) -> int:
        if isinstance(b, str):
            return const_node(b)
        if b not in driver_of:
            # undriven wire: model it as an unknown constant so the graph stays closed
            driver_of[b] = const_node("x")
        return driver_of[b]

    edges: list[dict] = []
    for nid, c in cell_nodes:
        node = nodes[nid]
        conns = c["connections"]
        if node["kind"] == "GATE":
            in_pins = GATE_INPUT_PINS[node["type"]]
            out_pins = ("Y",)
        else:
            out_pins = (DFF_OUTPUT_PIN,)
            in_pins = tuple(p for p in conns if p not in out_pins)
        node["inputs"] = {}
        for pin in in_pins:
            bits = conns.get(pin, [])
            if len(bits) != 1:
                raise ValueError(f"{c['type']} {node['cell']} pin {pin}: expected 1 bit, got {len(bits)}")
            d = driver(bits[0])
            node["inputs"][pin] = d
            edges.append({"from": d, "to": nid, "pin": pin})
        (ob,) = conns[out_pins[0]]
        node["output_bit"] = ob
        node["name"] = bit_name.get(ob) if isinstance(ob, int) else None
    for node in nodes:
        if node["kind"] == "OUTPUT":
            d = driver(node.pop("source_bit"))
            node["inputs"] = {"A": d}
            edges.append({"from": d, "to": node["id"], "pin": "A"})

    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "top": top_name,
        "ports": {n: {"direction": p["direction"], "width": len(p["bits"])} for n, p in ports.items()},
        "nodes": nodes,
        "edges": edges,
    }


def compute_metrics(graph: dict) -> dict:
    nodes = graph["nodes"]
    edges = graph["edges"]
    by_kind = Counter(n["kind"] for n in nodes)
    gate_counts = Counter(n["type"] for n in nodes if n["kind"] == "GATE")
    dff_counts = Counter(n["dff_type"] for n in nodes if n["kind"] == "DFF")

    # clock pins are fed by a clock tree and async set/reset pins by a reset network, not by
    # data logic: keep both out of the data fanout numbers and report them by driver instead
    id2node = {n["id"]: n for n in nodes}

    def dff_pin(e: dict) -> str | None:
        return e["pin"] if id2node[e["to"]]["kind"] == "DFF" else None

    data_edges = [e for e in edges if dff_pin(e) in (None, DFF_DATA_PIN)]
    fanout = Counter(e["from"] for e in data_edges)
    drivers = [n["id"] for n in nodes if n["kind"] in ("INPUT", "GATE", "DFF") or n["kind"].startswith("CONST")]
    fanouts = [fanout.get(d, 0) for d in drivers]

    def fanout_by_driver(pred) -> dict[str, int]:
        c = Counter(e["from"] for e in edges if pred(dff_pin(e)))
        return {id2node[d].get("name") or f"node{d}": n for d, n in sorted(c.items())}

    clocks = fanout_by_driver(lambda pin: pin == DFF_CLOCK_PIN)
    controls = fanout_by_driver(lambda pin: pin not in (None, DFF_DATA_PIN, DFF_CLOCK_PIN))

    # longest combinational path: DP over gates in topological order
    depth: dict[int, int] = {}
    indeg = {n["id"]: 0 for n in nodes}
    succ: dict[int, list[int]] = {n["id"]: [] for n in nodes}
    is_dff = {n["id"] for n in nodes if n["kind"] == "DFF"}
    for e in edges:
        if e["to"] in is_dff:
            continue  # registers cut the graph: their inputs are sinks, their outputs sources
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    ready = [n["id"] for n in nodes if indeg[n["id"]] == 0]
    order: list[int] = []
    while ready:
        nid = ready.pop()
        order.append(nid)
        for s in succ[nid]:
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
    comb_loop = len(order) != len(nodes)
    for nid in order:
        n = nodes[nid]
        if n["kind"] == "GATE":
            depth[nid] = 1 + max((depth.get(d, 0) for d in n["inputs"].values()), default=0)
        else:
            # sources (inputs, constants) and register outputs restart the count; sinks take
            # their drivers' depth for reporting but do not propagate (a DFF output is depth 0)
            depth[nid] = 0
    # data sinks only: an output port's A pin and a flop's D pin. Gates on a clock or async
    # set/reset path reach a DFF through another pin and are not flop-to-flop data depth.
    data_sink_pin = {"OUTPUT": "A", "DFF": DFF_DATA_PIN}
    sink_depths = [
        depth.get(n["inputs"][pin], 0)
        for n in nodes
        if (pin := data_sink_pin.get(n["kind"])) is not None and pin in n["inputs"]
    ]
    max_depth = max(sink_depths, default=0)

    m: dict = {k.lower(): gate_counts.get(k, 0) for k in GATE_INPUT_PINS}
    m["dff"] = by_kind.get("DFF", 0)
    m["gate_total"] = sum(gate_counts.values())
    m["node_total"] = len(nodes)
    m["edge_total"] = len(edges)
    m["max_depth"] = max_depth
    m["max_fanout"] = max(fanouts, default=0)
    m["avg_fanout"] = round(sum(fanouts) / len(fanouts), 4) if fanouts else 0.0
    m["clock_fanout"] = clocks
    m["control_fanout"] = controls
    m["input_bits"] = by_kind.get("INPUT", 0)
    m["output_bits"] = by_kind.get("OUTPUT", 0)
    m["constants"] = {k: by_kind.get(k, 0) for k in ("CONST0", "CONST1", "CONSTX") if by_kind.get(k)}
    m["gates_by_type"] = dict(sorted(gate_counts.items()))
    m["dff_by_type"] = dict(sorted(dff_counts.items()))
    m["combinational_loop"] = comb_loop
    return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("yosys_json", help="output of `write_json` after lowering to one-bit gates")
    ap.add_argument("--top", help="module to import (default: the only module)")
    ap.add_argument("-o", "--out", default="boolean_graph.json")
    ap.add_argument("--metrics", help="also write structural metrics JSON here")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help=f"synthesis profile whose dff_types allowlist to enforce (default: {DEFAULT_PROFILE})")
    ap.add_argument("--dff-type", action="append", metavar="CELL",
                    help="accept this $_DFF_* cell type instead of the profile's list (repeatable)")
    args = ap.parse_args(argv)
    try:
        dff_types = set(args.dff_type) if args.dff_type else load_profile_dff_types(args.profile)
        graph = build_graph(json.loads(Path(args.yosys_json).read_text()), args.top, dff_types)
    except (OSError, ValueError, KeyError) as e:
        print(f"[boolean_graph] error: {e}", file=sys.stderr)
        return 1
    metrics = compute_metrics(graph)
    try:
        Path(args.out).write_text(json.dumps(graph, indent=1, sort_keys=False) + "\n")
        if args.metrics:
            Path(args.metrics).write_text(json.dumps(metrics, indent=2) + "\n")
    except OSError as e:
        print(f"[boolean_graph] error: {e}", file=sys.stderr)
        return 1
    print(json.dumps({k: metrics[k] for k in ("gate_total", "dff", "edge_total", "max_depth", "max_fanout")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
