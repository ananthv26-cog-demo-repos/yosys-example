#!/usr/bin/env python3
"""
diff_sim: random-stimulus differential simulation of the RTL against the ASAP7-mapped
netlist with Icarus Verilog.

A self-checking testbench is generated from the port list: every input gets fresh pseudo-
random values each cycle (fixed seed => reproducible), clocks toggle, resets are held
through the first `reset_cycles` rising edges of the primary clock, and from then on every
output bit the RTL drives to 0/1 must match the netlist. Inputs change with non-blocking
assignments on the primary clock's falling edge, so a flop in any domain whose edge
coincides with that instant samples the previous value on both sides.
Gate-level X where the RTL is known is reported separately as `gate_x_bits` (X-pessimism
of the cell models, not a mismatch); only a netlist that never drives a known value where
the RTL does is a failure. Callers that want X to be fatal check `gate_x_bits` themselves.

Sampling is driven by the first clock: `cycles` counts its periods, and every clock toggles
with a distinct period so multi-clock blocks see edges in both orders. Values are compared
only where the RTL is known, so clock-domain crossings are checked functionally at the
primary clock's rate, not for CDC timing.

The RTL is converted with sv2v first (Icarus' SystemVerilog support is thin); the mapped
netlist simulates against Verilog cell models that Yosys derives from the Liberty
`function` / `ff` groups, so no vendor models are needed.

This is a sample of behaviour, not a proof; bool_area.py's formal equivalence checks are
the proof. It exists because "matches RTL simulation" is the criterion designers
actually apply, and because it also exercises the Liberty cell functions in simulation.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

CLOCK_RE = re.compile(r"clk|clock", re.IGNORECASE)
NOT_CLOCK_RE = re.compile(r"(en|enable|gate|sel|div|ok|valid)(_i)?$", re.IGNORECASE)
# a reset is a whole name token (split on `_` and camelCase): `rst`, `reset`, `arst_n`, `wrst_ni`, `hresetn`,
# `soft_rst`, `RstN`; not a substring inside an ordinary word (`burst_i`, `first`)
RESET_TOKEN_RE = re.compile(r"^(?:[a-z]|hw|sw|por|soft|sync|async)?(?:rst|reset)(?:n|ni|i|in|b|l)?$")
ACTIVE_LOW_RE = re.compile(r"(_n|_ni|_b|_l|n)$", re.IGNORECASE)


def is_reset_name(name: str) -> bool:
    tokens = re.split(r"_+|(?<=[a-z0-9])(?=[A-Z])", name)
    return any(RESET_TOKEN_RE.match(t.lower()) for t in tokens if t)


def classify_ports(ports: dict[str, dict], clocks: list[str] | None = None,
                   resets: dict[str, bool] | None = None) -> tuple[list[str], dict[str, bool], list[str], list[str]]:
    """-> (clocks, {reset: active_low}, data inputs, outputs).

    Clocks / resets are guessed from 1-bit input names unless given explicitly (each independently:
    an explicit clock list still infers resets, and vice versa). A name matching both patterns
    (`clk_reset_n`) is a reset: reset syntax wins over clock syntax. Raises on inout."""
    infer_clocks, infer_resets = clocks is None, resets is None
    clocks, resets = list(clocks or []), dict(resets or {})
    for name in [*clocks, *resets]:
        if name not in ports or ports[name]["direction"] != "input":
            raise ValueError(f"{name}: not an input port")
    data_in, outs = [], []
    for name, p in ports.items():
        if p["direction"] == "inout":
            raise ValueError(f"inout port {name}: differential simulation not supported")
        if p["direction"] == "output":
            outs.append(name)
        elif name in clocks or name in resets:
            continue
        elif infer_resets and p["width"] == 1 and is_reset_name(name):
            resets[name] = bool(ACTIVE_LOW_RE.search(name))
        elif infer_clocks and p["width"] == 1 and CLOCK_RE.search(name) and not NOT_CLOCK_RE.search(name):
            clocks.append(name)
        else:
            data_in.append(name)
    return clocks, resets, data_in, outs


def gen_testbench(top: str, ports: dict[str, dict], cycles: int, seed: int, reset_cycles: int = 4,
                  gate_suffix: str = "__gate", clocks: list[str] | None = None,
                  resets: dict[str, bool] | None = None) -> str:
    clocks, resets, data_in, outs = classify_ports(ports, clocks, resets)
    w = {n: p["width"] for n, p in ports.items()}

    def decl(kind: str, name: str, width: int, suffix: str = "") -> str:
        rng = f"[{width - 1}:0] " if width > 1 else ""
        return f"  {kind} {rng}{name}{suffix};"

    lines = ["`timescale 1ns/1ps", "module tb;", f"  integer seed = {seed};", "  integer cycle = 0;",
             "  integer mismatches = 0;", "  integer gate_x_bits = 0;", "  integer compared_bits = 0;", "  integer i;"]
    for n in ports:
        if ports[n]["direction"] == "input":
            lines.append(decl("reg", n, w[n]))
        else:
            lines.append(decl("wire", n, w[n], "_gold"))
            lines.append(decl("wire", n, w[n], gate_suffix))
    conn_gold = ", ".join(f".{n}({n}{'_gold' if ports[n]['direction'] == 'output' else ''})" for n in ports)
    conn_gate = ", ".join(f".{n}({n}{gate_suffix if ports[n]['direction'] == 'output' else ''})" for n in ports)
    lines.append(f"  {top} gold({conn_gold});")
    lines.append(f"  {top}{gate_suffix} gate({conn_gate});")

    # clocks: distinct periods so multi-clock blocks see edges in both orders
    periods = [10, 14, 18, 22]
    if not clocks:
        lines.append("  reg tb_clk;")
    tick = clocks[0] if clocks else "tb_clk"
    lines.append("  initial begin")
    for i, c in enumerate(clocks or ["tb_clk"]):
        lines.append(f"    {c} = 0;")
    for r, low in resets.items():
        lines.append(f"    {r} = {0 if low else 1};")
    for n in data_in:
        lines.append(f"    {n} = 0;")
    lines.append("  end")
    for i, c in enumerate(clocks or ["tb_clk"]):
        half = periods[i % len(periods)] // 2
        lines.append(f"  always #{half} {c} = ~{c};")

    def rand_expr(width: int) -> str:
        if width <= 32:
            return f"$random(seed)" if width == 32 else f"$random(seed) & {{{width}{{1'b1}}}}"
        words = (width + 31) // 32
        return "{" + ", ".join("$random(seed)" for _ in range(words)) + "}"

    lines.append(f"  always @(negedge {tick}) begin")
    lines.append(f"    if (cycle >= {reset_cycles}) begin")
    for n in outs:
        g, t = (f"{n}_gold", f"{n}{gate_suffix}") if w[n] == 1 else (f"{n}_gold[i]", f"{n}{gate_suffix}[i]")
        lines.append(f"      for (i = 0; i < {w[n]}; i = i + 1) begin")
        lines.append(f"        if ({g} !== 1'bx && {g} !== 1'bz) begin")
        lines.append("          compared_bits = compared_bits + 1;")
        lines.append(f"          if ({t} === 1'bx || {t} === 1'bz) gate_x_bits = gate_x_bits + 1;")
        lines.append(f"          else if ({t} !== {g}) begin")
        lines.append("            mismatches = mismatches + 1;")
        lines.append(f"            if (mismatches <= 10) $display(\"MISMATCH cycle=%0d {n}[%0d] rtl=%b gate=%b\", "
                     f"cycle, i, {g}, {t});")
        lines.append("          end")
        lines.append("        end")
        lines.append("      end")
    lines.append("    end")
    lines.append("    cycle = cycle + 1;")
    # non-blocking: a secondary clock whose edge coincides with this falling edge must see the
    # old value in both the RTL and the gate model, not race the update
    for n in data_in:
        lines.append(f"    {n} <= {rand_expr(w[n])};")
    for r, low in resets.items():
        # reset is active from time 0 and released at falling edge `reset_cycles`, i.e. exactly the first
        # `reset_cycles` rising edges see it; afterwards it pulses rarely so reset logic is also compared
        active, inactive = ("0", "1") if low else ("1", "0")
        lines.append(f"    {r} <= (cycle < {reset_cycles} || ($random(seed) & 63) == 0) ? 1'b{active} : 1'b{inactive};")
    lines.append(f"    if (cycle == {cycles}) begin")
    lines.append('      $display("DIFFSIM cycles=%0d compared_bits=%0d mismatches=%0d gate_x_bits=%0d", '
                 "cycle, compared_bits, mismatches, gate_x_bits);")
    lines.append("      $finish;")
    lines.append("    end")
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def parse_result(stdout: str) -> dict:
    m = re.search(r"DIFFSIM cycles=(\d+) compared_bits=(\d+) mismatches=(\d+) gate_x_bits=(\d+)", stdout)
    if not m:
        return {"status": "failed", "error": "simulation produced no summary line"}
    cycles, compared, mism, gx = (int(g) for g in m.groups())
    res = {"cycles": cycles, "compared_bits": compared, "mismatches": mism, "gate_x_bits": gx}
    if mism:
        res["status"] = "mismatch"
        res["first_mismatches"] = [ln for ln in stdout.splitlines() if ln.startswith("MISMATCH")][:10]
    elif compared == 0:
        res["status"] = "failed"
        res["error"] = "RTL never drove a known output value; nothing was compared"
    elif gx == compared:
        res["status"] = "failed"
        res["error"] = "netlist never drove a known value where the RTL did; nothing was compared"
    else:
        res["status"] = "match"
    return res


def run_diff_sim(*, top: str, ports: dict, rtl_sources: list[str], includes: list[str], defines: list[str],
                 mapped_json: Path, libs: list[Path], yosys: str, sv2v: str | None, iverilog: str, vvp: str,
                 work: Path, cycles: int, seed: int, timeout: int, clocks: list[str] | None = None,
                 resets: dict[str, bool] | None = None) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    res: dict = {"status": "failed", "cycles_requested": cycles, "seed": seed, "work_dir": str(work)}
    if cycles < 1:
        res["error"] = f"cycles must be at least 1, got {cycles}"
        return res
    try:
        clk, rst, _, _ = classify_ports(ports, clocks, resets)
        res["clocks"] = clk
        res["resets"] = {r: "active_low" if low else "active_high" for r, low in rst.items()}
        tb = gen_testbench(top, ports, cycles, seed, clocks=clk, resets=rst)
    except ValueError as e:
        res["error"] = str(e)
        return res
    (work / "tb.v").write_text(tb)
    try:
        return _simulate(res, top=top, rtl_sources=rtl_sources, includes=includes, defines=defines,
                         mapped_json=mapped_json, libs=libs, yosys=yosys, sv2v=sv2v, iverilog=iverilog, vvp=vvp,
                         work=work, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        res["error"] = f"{Path(e.cmd[0]).name} timed out after {timeout}s"
        return res
    except OSError as e:
        res["error"] = f"cannot run {e.filename or 'simulation tool'}: {e.strerror or e}"
        return res


def _simulate(res: dict, *, top: str, rtl_sources: list[str], includes: list[str], defines: list[str],
              mapped_json: Path, libs: list[Path], yosys: str, sv2v: str | None, iverilog: str, vvp: str,
              work: Path, timeout: int) -> dict:
    # gate side: mapped netlist (renamed) + Liberty-derived cell models, one Verilog file
    gate_v = work / "gate.v"
    script = "\n".join([
        *[f"read_liberty -ignore_miss_func -ignore_miss_dir -ignore_miss_data_latch {shlex.quote(str(p))}" for p in libs],
        f"read_json {shlex.quote(str(mapped_json))}",
        f"hierarchy -top {top}",
        f"rename {top} {top}__gate",
        f"write_verilog -noattr {shlex.quote(str(gate_v))}",
        "",
    ])
    (work / "gate.ys").write_text(script)
    p = subprocess.run([yosys, "-q", "-s", str(work / "gate.ys")], capture_output=True, text=True, timeout=timeout,
                       check=False)
    if p.returncode != 0 or not gate_v.exists():
        res["error"] = "could not write gate-level simulation model: " + (p.stderr.strip().splitlines() or ["?"])[-1]
        return res

    # gold side: RTL through sv2v when available (Icarus reads plain Verilog reliably)
    gold_sources = list(rtl_sources)
    iv_flags = ["-g2012"]
    if sv2v:
        gold_v = work / "gold.v"
        cmd = [sv2v, *[f"-I{i}" for i in includes], *[f"-D{d}" for d in defines], f"--top={top}", "-w", str(gold_v),
               *rtl_sources]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if p.returncode == 0:
            gold_sources, iv_flags = [str(gold_v)], ["-g2005"]
            res["gold"] = "sv2v"
        else:
            res["gold"] = "rtl (sv2v failed: " + (p.stderr.strip().splitlines() or ["?"])[-1] + ")"
    else:
        res["gold"] = "rtl"
    if res["gold"] != "sv2v":
        iv_flags += [f"-I{i}" for i in includes] + [f"-D{d}" for d in defines]

    vvp_file = work / "sim.vvp"
    # -s tb: only the generated testbench is a root, so uninstantiated modules in the RTL sources
    # (with their own initial blocks) cannot run alongside or end the simulation
    cmd = [iverilog, *iv_flags, "-s", "tb", "-o", str(vvp_file), str(work / "tb.v"), *gold_sources, str(gate_v)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    (work / "iverilog.log").write_text(p.stdout + p.stderr)
    if p.returncode != 0:
        res["error"] = "iverilog failed: " + ((p.stderr or p.stdout).strip().splitlines() or ["?"])[-1]
        return res
    p = subprocess.run([vvp, "-n", str(vvp_file)], capture_output=True, text=True, timeout=timeout, check=False)
    (work / "sim.log").write_text(p.stdout + p.stderr)
    if p.returncode != 0:
        res["error"] = f"vvp exited {p.returncode}: " + ((p.stderr or p.stdout).strip().splitlines() or ["?"])[-1]
        return res
    res.update(parse_result(p.stdout))
    return res
