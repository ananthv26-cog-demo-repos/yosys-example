#!/usr/bin/env python3
"""
synth_area: synthesize one SystemVerilog block with Yosys and emit a JSON report.

    synth_area.py --top fifo fifo.sv [more.sv ...] [--liberty cells.lib] -o report.json

The report is meant to be the fast, machine-readable proxy for "did this RTL
change make the block bigger or smaller" that an auto-research loop or a human
can consume, with a slower commercial synthesis flow as occasional ground truth.

Frontends (how SystemVerilog gets into Yosys):
  slang    built-in `read_slang` (slang-based SV elaborator shipped with Yosys)
  sv2v     external `sv2v` converts SV -> Verilog-2005, then `read_verilog -sv`
  verilog  Yosys' native `read_verilog -sv` (limited SV subset)
  auto     slang, falling back to sv2v, falling back to verilog (default)

The report is always written, even on failure; the exit code is 0 only when
synthesis completed and produced statistics.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

FRONTENDS = ("auto", "slang", "sv2v", "verilog")

# sequential cells are recognised by type name (Yosys' $_DFF_*/$_SDFF_*/$_DLATCH_* and the DFF/LATCH
# naming most Liberty libraries use), not from Liberty ff()/latch() groups: a library whose flops are
# named differently shows them under num_comb_cells, so num_flops is only comparable within one library
FLOP_RE = re.compile(r"(DFF|SDFF|ADFF|DLATCH|LATCH|_FF_|\bDFF)", re.IGNORECASE)
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
UNKNOWN_AREA_RE = re.compile(r"^\s*Area for cell type (.+?) is unknown!\s*$", re.MULTILINE)


@dataclass
class StageResult:
    name: str
    ok: bool
    seconds: float
    command: str
    returncode: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class Report:
    schema_version: int = SCHEMA_VERSION
    status: str = "failed"
    top: str | None = None
    frontend_requested: str = "auto"
    frontend_used: str | None = None
    frontends_tried: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    liberty: str | None = None
    flatten: bool = True
    yosys: str | None = None
    yosys_version: str | None = None
    sv2v_version: str | None = None
    work_dir: str | None = None
    log: str | None = None
    netlist: str | None = None
    stats: dict = field(default_factory=dict)
    stages: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wall_seconds: float = 0.0
    started_at: float = field(default_factory=time.time)


def absolutize(tool: str | None) -> str | None:
    """A tool path given relative to the caller's cwd must stay valid when stages run in the work dir;
    bare names (no separator) are left to PATH lookup, which does not depend on cwd."""
    if tool and os.sep in tool and not os.path.isabs(tool):
        return str(Path(tool).resolve())
    return tool


def find_yosys(explicit: str | None) -> str | None:
    if explicit:
        return absolutize(explicit)
    for cand in (
        absolutize(os.environ.get("YOSYS")),
        str(REPO_ROOT / "build" / "yosys"),
        shutil.which("yosys"),
    ):
        if cand and Path(cand).exists():
            return cand
    return None


def find_sv2v(explicit: str | None) -> str | None:
    return absolutize(explicit or os.environ.get("SV2V")) or shutil.which("sv2v")


def tail(text: str, n: int = 40) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def run(cmd: list[str], name: str, cwd: Path, timeout: int | None, report: Report) -> StageResult:
    t0 = time.time()
    cmdline = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        res = StageResult(
            name=name,
            ok=proc.returncode == 0,
            seconds=time.time() - t0,
            command=cmdline,
            returncode=proc.returncode,
            stdout_tail=tail(proc.stdout),
            stderr_tail=tail(proc.stderr),
        )
    except subprocess.TimeoutExpired as e:
        res = StageResult(
            name=name,
            ok=False,
            seconds=time.time() - t0,
            command=cmdline,
            returncode=None,
            stdout_tail=tail(e.stdout or "" if isinstance(e.stdout, str) else ""),
            stderr_tail=f"timeout after {timeout}s",
        )
    except OSError as e:
        res = StageResult(name=name, ok=False, seconds=0.0, command=cmdline, stderr_tail=str(e))
    report.stages.append(res.__dict__)
    return res


def tool_version(binary: str | None, flag: str) -> str | None:
    if not binary:
        return None
    try:
        out = subprocess.run([binary, flag], capture_output=True, text=True, timeout=30, check=False)
        first = (out.stdout or out.stderr).strip().splitlines()
        return first[0] if first else None
    except (OSError, subprocess.SubprocessError):
        return None


GENERIC_ERRORS = ("Design elaboration failed", "Build failed:")


def extract_yosys_diagnostics(log_text: str) -> tuple[list[str], list[str]]:
    """Return (warnings, errors); specific diagnostics come before the generic
    'elaboration failed' summary so errors[0] is the actionable one."""
    warnings, errors, generic = [], [], []
    for line in log_text.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("warning:") or ": warning:" in low:
            warnings.append(s)
        elif low.startswith("error:") or ": error:" in low or s.startswith("ERROR:"):
            (generic if any(g in s for g in GENERIC_ERRORS) else errors).append(s)
    seen: set[str] = set()
    warnings = [w for w in warnings if not (w in seen or seen.add(w))]
    return warnings, errors + generic


def yosys_script(
    read_cmd: str, top: str, liberty: str | None, flatten: bool, stat_json: Path, netlist: Path | None,
    stat_txt: Path | None = None,
) -> str:
    lines = [read_cmd, f"hierarchy -check -top {top}"]
    synth = f"synth -top {top}" + (" -flatten" if flatten else "")
    lines.append(synth)
    if liberty:
        lib = shlex.quote(liberty)
        lines += [
            f"dfflibmap -liberty {lib}",
            f"abc -liberty {lib}",
            "opt_clean -purge",
            f"tee -q -o {shlex.quote(str(stat_json))} stat -liberty {lib} -json",
        ]
        if stat_txt:
            # the JSON has no list of cells the library gave no area for; the text report does
            lines.append(f"tee -q -o {shlex.quote(str(stat_txt))} stat -liberty {lib}")
    else:
        lines += [
            "opt_clean -purge",
            f"tee -q -o {shlex.quote(str(stat_json))} stat -tech cmos -json",
        ]
    if netlist:
        lines.append(f"write_verilog -noattr {shlex.quote(str(netlist))}")
    if liberty and stat_txt:
        # stat only does area bookkeeping when the library gave it >= 1 `area`; ask yosys' own
        # liberty reader which cells carry one (none => nothing above was counted). List modules,
        # not `-count` objects: a pinless cell has no members but still has an area.
        lines += [
            "design -reset",
            f"read_liberty -lib {shlex.quote(liberty)}",
            f"tee -q -o {shlex.quote(str(lib_area_probe(stat_txt)))} select -list-mod =A:area",
        ]
    return "\n".join(lines) + "\n"


SLANG_SYNTH_DEFAULTS = ["--ignore-assertions", "--ignore-initial", "--ignore-timing"]


def read_cmd_for(frontend: str, sources: list[str], includes: list[str], defines: list[str], top: str,
                 slang_args: list[str] | None = None) -> str:
    q = [shlex.quote(s) for s in sources]
    if frontend == "slang":
        opts = [f"-I{shlex.quote(i)}" for i in includes] + [f"-D{shlex.quote(d)}" for d in defines]
        opts += [shlex.quote(a) for a in (slang_args or [])]
        return " ".join(["read_slang", "--top", top, *opts, *q])
    if frontend in ("sv2v", "verilog"):
        opts = [f"-I{shlex.quote(i)}" for i in includes] + [f"-D{shlex.quote(d)}" for d in defines]
        return " ".join(["read_verilog", "-sv", *opts, *q])
    raise ValueError(frontend)


def unescape_id(rtlil_id: str) -> str:
    """Mirror of RTLIL::IdString::unescape(): drop the leading `\\` of a public id unless
    the rest would be ambiguous with an internal (`$`), escaped (`\\`) or numeric name."""
    if len(rtlil_id) < 2 or rtlil_id[0] != "\\" or rtlil_id[1] in "$\\" or rtlil_id[1].isdigit():
        return rtlil_id
    return rtlil_id[1:]


def unknown_area_cells(stat_txt: Path | None) -> set[str]:
    """Cell types `stat -liberty` reported as having no area (it leaves them out of the total).
    The text report prints RTLIL ids; `stat -json` keys are unescaped, so match that."""
    if not stat_txt or not stat_txt.exists():
        return set()
    return {unescape_id(t) for t in UNKNOWN_AREA_RE.findall(stat_txt.read_text(errors="replace"))}


def lib_area_probe(stat_txt: Path) -> Path:
    return stat_txt.with_name("lib_area_cells.txt")


def liberty_has_area(stat_txt: Path | None) -> bool | None:
    """Whether yosys' liberty reader found any cell with an `area` attribute (None if unprobed)."""
    probe = lib_area_probe(stat_txt) if stat_txt else None
    if not probe or not probe.exists():
        return None
    return any(line.strip() for line in probe.read_text(errors="replace").splitlines())


def summarize_stats(stat_json: Path, top: str, liberty: str | None, stat_txt: Path | None = None) -> dict:
    text = stat_json.read_text()
    # the liberty reader logs "Found gzip magic ..." lines through `tee` ahead of the JSON
    data = json.loads(text[text.index("{"):] if "{" in text else text)
    # `stat -json` emits {"creator":..., "design": {...}, "modules": {...}}.
    design = data.get("design") or {}
    if not design:
        mods = data.get("modules", {})
        design = mods.get(top) or mods.get(f"\\{top}") or (next(iter(mods.values())) if mods else {})
    cells_by_type: dict[str, int] = dict(design.get("num_cells_by_type", {}))
    num_cells = int(design.get("num_cells", sum(cells_by_type.values())))
    flops = sum(n for t, n in cells_by_type.items() if FLOP_RE.search(t))
    summary = {
        "num_cells": num_cells,
        "num_flops": flops,
        "num_comb_cells": num_cells - flops,
        "num_wires": design.get("num_wires"),
        "num_wire_bits": design.get("num_wire_bits"),
        "num_ports": design.get("num_ports"),
        "num_port_bits": design.get("num_port_bits"),
        "num_memories": design.get("num_memories"),
        "num_memory_bits": design.get("num_memory_bits"),
        "cells_by_type": dict(sorted(cells_by_type.items(), key=lambda kv: -kv[1])),
    }
    if liberty:
        unknown = sorted(t for t in cells_by_type if t in unknown_area_cells(stat_txt))
        summary["area"] = float(design.get("area", 0.0))
        if liberty_has_area(stat_txt) is False:
            unknown = sorted(cells_by_type)
        summary["area_unit"] = "liberty area units"
        summary["sequential_area"] = design.get("sequential_area")
        summary["area_is_lower_bound"] = bool(unknown)
        summary["unknown_area_cell_types"] = {t: cells_by_type[t] for t in unknown}
    else:
        # yosys reports e.g. "6222+" when some cells have no transistor model
        raw = str(design.get("estimated_num_transistors", "")).strip()
        digits = re.match(r"\d+", raw)
        summary["estimated_transistors"] = int(digits.group()) if digits else None
        summary["estimated_transistors_is_lower_bound"] = raw.endswith("+")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="SystemVerilog / Verilog source files (packages first)")
    ap.add_argument("--top", required=True, help="top module name")
    ap.add_argument("-I", "--include", action="append", default=[], help="include directory")
    ap.add_argument("-D", "--define", action="append", default=[], help="preprocessor define NAME[=VALUE]")
    ap.add_argument("--frontend", choices=FRONTENDS, default="auto")
    ap.add_argument("--liberty", help="Liberty .lib for technology mapping and area (omit for generic gate counts)")
    ap.add_argument("--slang-arg", action="append", default=[], metavar="ARG",
                    help="extra read_slang option, repeatable (e.g. --slang-arg=--allow-use-before-declare)")
    ap.add_argument("--empty-blackboxes", action="store_true",
                    help="treat modules with empty bodies (stubs for tech cells, SRAM macros) as black boxes (slang only)")
    ap.add_argument("--keep-assertions", action="store_true",
                    help=f"do not pass {' '.join(SLANG_SYNTH_DEFAULTS)} to read_slang")
    ap.add_argument("--no-flatten", action="store_true", help="keep hierarchy (default: flatten)")
    ap.add_argument("-o", "--out", default="report.json", help="report path (default: report.json)")
    ap.add_argument("--work-dir", help="scratch dir for logs/netlist (default: <out>.work)")
    ap.add_argument("--netlist", action="store_true", help="also write the mapped netlist as Verilog")
    ap.add_argument("--yosys", help="path to yosys binary (default: $YOSYS, ./build/yosys, PATH)")
    ap.add_argument("--sv2v", help="path to sv2v binary (default: $SV2V, PATH)")
    ap.add_argument("--timeout", type=int, default=3600, help="per-stage timeout in seconds")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    if not IDENT_RE.match(args.top):
        ap.error(f"--top must be a plain module identifier, got {args.top!r}")

    rep = Report()
    rep.top = args.top
    rep.frontend_requested = args.frontend
    rep.sources = [str(Path(s).resolve()) for s in args.sources]
    rep.include_dirs = [str(Path(i).resolve()) for i in args.include]
    rep.defines = list(args.define)
    rep.liberty = str(Path(args.liberty).resolve()) if args.liberty else None
    rep.flatten = not args.no_flatten

    out = Path(args.out).resolve()
    work = Path(args.work_dir).resolve() if args.work_dir else out.with_suffix(".work")
    work.mkdir(parents=True, exist_ok=True)
    rep.work_dir = str(work)

    def finish(code: int) -> int:
        rep.wall_seconds = round(time.time() - rep.started_at, 3)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep.__dict__, indent=2, sort_keys=False) + "\n")
        if not args.quiet:
            s = rep.stats
            if rep.status == "ok":
                area = f" area={s['area']}" if "area" in s else f" ~transistors={s.get('estimated_transistors')}"
                print(
                    f"[synth_area] OK top={rep.top} frontend={rep.frontend_used} "
                    f"cells={s['num_cells']} flops={s['num_flops']}{area} ({rep.wall_seconds}s) -> {out}"
                )
            else:
                print(f"[synth_area] FAILED top={rep.top}: {rep.errors[0] if rep.errors else 'see report'} -> {out}",
                      file=sys.stderr)
        return code

    for s in rep.sources:
        if not Path(s).exists():
            rep.errors.append(f"source not found: {s}")
    if rep.liberty and not Path(rep.liberty).exists():
        rep.errors.append(f"liberty not found: {rep.liberty}")
    if rep.errors:
        return finish(2)

    yosys = find_yosys(args.yosys)
    if not yosys or not (Path(yosys).is_file() or shutil.which(yosys)):
        rep.errors.append(f"yosys binary not found{f': {yosys}' if yosys else ''} "
                          "(build the repo, set $YOSYS, or pass --yosys)")
        return finish(2)
    rep.yosys = yosys
    rep.yosys_version = tool_version(yosys, "-V")
    sv2v = find_sv2v(args.sv2v)
    rep.sv2v_version = tool_version(sv2v, "--version")

    order = ["slang", "sv2v", "verilog"] if args.frontend == "auto" else [args.frontend]
    stat_json = work / "stat.json"
    stat_txt = work / "stat.txt" if rep.liberty else None
    netlist = work / "netlist.v" if args.netlist else None

    slang_only = args.slang_arg or args.empty_blackboxes or args.keep_assertions
    if slang_only and order != ["slang"]:
        rep.warnings.append("slang-only options (--slang-arg/--empty-blackboxes/--keep-assertions) are ignored "
                            "by the sv2v and verilog frontends")

    for fe in order:
        rep.frontends_tried.append(fe)
        sources = rep.sources
        if fe == "sv2v":
            if not sv2v:
                rep.warnings.append("sv2v not found; skipping sv2v frontend")
                continue
            conv = work / "sv2v_out.v"
            cmd = [sv2v, *[f"-I{i}" for i in rep.include_dirs], *[f"-D{d}" for d in rep.defines],
                   f"--top={args.top}", "-w", str(conv), *sources]
            r = run(cmd, "sv2v", work, args.timeout, rep)
            if not r.ok:
                rep.errors.append(f"sv2v failed: {tail(r.stderr_tail, 5)}")
                continue
            sources = [str(conv)]
            includes, defines = [], []
        else:
            includes, defines = rep.include_dirs, rep.defines

        slang_args = ([] if args.keep_assertions else SLANG_SYNTH_DEFAULTS) + args.slang_arg
        if args.empty_blackboxes:
            slang_args = [*slang_args, "--empty-blackboxes"]
        script = yosys_script(
            read_cmd_for(fe, sources, includes, defines, args.top, slang_args),
            args.top, rep.liberty, rep.flatten, stat_json, netlist, stat_txt,
        )
        script_path = work / f"synth_{fe}.ys"
        script_path.write_text(script)
        log_path = work / f"yosys_{fe}.log"
        for stale in (stat_json, stat_txt, lib_area_probe(stat_txt) if stat_txt else None):
            if stale and stale.exists():
                stale.unlink()
        cmd = [yosys, "-q", "-T", "-l", str(log_path), "-s", str(script_path)]
        r = run(cmd, f"yosys[{fe}]", work, args.timeout, rep)
        log_text = log_path.read_text() if log_path.exists() else ""
        w, e = extract_yosys_diagnostics(log_text)
        rep.log = str(log_path)
        if r.ok and stat_json.exists():
            rep.frontend_used = fe
            # earlier frontends' failures are context, not errors, once one succeeds
            rep.warnings.extend(f"earlier attempt failed: {m}" for m in rep.errors)
            rep.errors = []
            rep.warnings.extend(w)
            try:
                rep.stats = summarize_stats(stat_json, args.top, rep.liberty, stat_txt)
            except (ValueError, KeyError) as ex:
                rep.errors.append(f"could not parse stat output: {ex}")
                break
            rep.netlist = str(netlist) if netlist else None
            rep.status = "ok"
            return finish(0)
        if e:
            rep.errors.extend(f"[{fe}] {m}" for m in e)
        else:
            rep.errors.append(f"[{fe}] {tail(r.stderr_tail or r.stdout_tail, 3) or f'yosys exited {r.returncode}'}")
        if fe == "slang" and "No such command: read_slang" in log_text:
            rep.warnings.append("this yosys was built without the slang frontend (YOSYS_WITHOUT_SLANG)")

    return finish(1)


if __name__ == "__main__":
    sys.exit(main())
