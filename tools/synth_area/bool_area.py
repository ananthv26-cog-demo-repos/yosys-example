#!/usr/bin/env python3
"""
bool_area: Yosys Boolean-layer area prototype, v1 flow.

    bool_area.py fifo.sv [pkg.sv ...] --top fifo -o out/fifo/

    SystemVerilog --slang--> word-level RTLIL --synth/abc--> generic one-bit Boolean gates
                  --dfflibmap/abc--> ASAP7 cells      (NOT AND NAND OR NOR XOR XNOR MUX + DFF)

Writes into the output directory:

    word_yosys.json      write_json after `proc; flatten; opt_dff` (multi-bit RTLIL cells)
    word_level.json      word-level operations (ADD/EQ/MUX/REG/MEMRD ...), widths, signedness,
                         operand signals, memories, source locations
    sequential_overlay.json  per register: clock/edge, reset kind/polarity/value, enable, init,
                         RTL name of every Q/D bit, source location
    generic_yosys.json   write_json of the generic Boolean-gate netlist
    mapped_yosys.json    write_json of the ASAP7-mapped netlist
    mapped_netlist.v     same, as structural Verilog
    boolean_graph.json   explicit graph: nodes (inputs/outputs/consts/gates/DFFs), edges
    metrics.json         gate counts, DFFs, mapped cells, area, edges, depth, fanout, status,
                         tool + profile versions and hashes, wall-clock
    yosys.log            full Yosys log of the synthesis run
    equiv_*.ys/.log      formal equivalence scripts + logs (RTL vs graph, graph vs mapped)

Exit status is non-zero when any stage (parse/elaboration, lowering, mapping, graph
validation, equivalence when enabled, artifact writing) fails; `metrics.json` is still
written with `status` and `errors` so batch runs can be triaged from the JSON alone.

Every knob that affects the numbers lives in one versioned profile
(profiles/<name>.json): frontend flags, gate set, pass order, Liberty file list + SHA-256.
Numbers are only comparable between runs of the same profile version.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path

from boolean_graph import GRAPH_SCHEMA_VERSION, UnsupportedCell, build_graph, compute_metrics
from diff_sim import classify_ports, run_diff_sim
from mapped_cells import MAPPED_SCHEMA_VERSION, build_cells, cell_types, load_liberty
from run_report import build_manifest, render_summary, sha256_file
from sequential_overlay import SEQ_SCHEMA_VERSION, build_overlay
from synth_area import (
    IDENT_RE,
    SLANG_SYNTH_DEFAULTS,
    extract_yosys_diagnostics,
    find_sv2v,
    find_yosys,
    read_cmd_for,
    summarize_stats,
    tool_version,
)
from word_level import WORD_SCHEMA_VERSION, build_report

METRICS_SCHEMA_VERSION = 2
HERE = Path(__file__).resolve().parent
PROFILES_DIR = HERE / "profiles"
DEFAULT_PROFILE = "asap7_rvt_tt_v1"
FRONTENDS = ("slang", "sv2v", "verilog")
LIBERTY_READ_FLAGS = "-ignore_miss_func -ignore_miss_dir -ignore_miss_data_latch"

ARTIFACTS = {
    "word_script": "word.ys",
    "word_log": "word.log",
    "word_json": "word_yosys.json",
    "word": "word_level.json",
    "sequential": "sequential_overlay.json",
    "generic_json": "generic_yosys.json",
    "mapped_json": "mapped_yosys.json",
    "netlist": "mapped_netlist.v",
    "graph": "boolean_graph.json",
    "mapped_cells": "mapped_cells.json",
    "metrics": "metrics.json",
    "manifest": "run_manifest.json",
    "summary": "summary.md",
    "log": "yosys.log",
    "script": "synth.ys",
    "stat_json": "stat.json",
    "stat_txt": "stat.txt",
}
SV2V_OUT = "sv2v_out.v"
SIM_DIR = "sim"
EQUIV_GLOB = "equiv_*.ys", "equiv_*.log"


def owned_files(out_dir: Path) -> list[Path]:
    """Every path this flow may write below `out_dir`: the fixed artifacts (whether or not they exist), the
    sv2v output, the equivalence scripts/logs and every file currently under the simulation directory."""
    owned = [out_dir / v for v in ARTIFACTS.values()] + [out_dir / SV2V_OUT]
    for pattern in EQUIV_GLOB:
        owned.extend(sorted(out_dir.glob(pattern)))
    sim_dir = out_dir / SIM_DIR
    if sim_dir.is_dir() and not sim_dir.is_symlink():
        owned.extend(p for p in sorted(sim_dir.rglob("*")) if p.is_file() or p.is_symlink())
    return owned


def purge_outputs(out_dir: Path) -> None:
    """Remove every file this flow owns so a rerun can never leave a previous run's results behind."""
    for p in owned_files(out_dir):
        if p.is_symlink() or p.is_file():
            p.unlink()
    sim_dir = out_dir / SIM_DIR
    if sim_dir.is_symlink() or sim_dir.is_file():
        sim_dir.unlink()
    elif sim_dir.is_dir():
        shutil.rmtree(sim_dir)


class FlowError(Exception):
    def __init__(self, stage: str, msg: str):
        super().__init__(msg)
        self.stage = stage


PROFILE_SCHEMA = {
    "name": str, "version": (int, str), "frontend": dict, "boolean_gates": list, "abc_gates": str,
    "dff_types": list, "liberty": dict, "script": dict,
}
SCRIPT_PLACEHOLDERS = ("top", "abc_gates", "dfflegalize_cells", "liberty_args", "word_json", "generic_json",
                       "mapped_json", "netlist", "stat_json", "stat_txt")
SCRIPT_STAGES = ("word", "lower", "map")


def all_strings(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def bad_placeholders(cmds: list[str]) -> list[str]:
    """Format fields in yosys command templates that are not exactly one of SCRIPT_PLACEHOLDERS."""
    bad: list[str] = []
    for cmd in cmds:
        try:
            fields = [(f, spec, conv) for _, f, spec, conv in string.Formatter().parse(cmd) if f is not None]
        except ValueError as e:
            bad.append(f"{cmd!r} ({e})")
            continue
        bad.extend(f"{{{f}}}" for f, spec, conv in fields if f not in SCRIPT_PLACEHOLDERS or spec or conv is not None)
    return bad


def load_profile(name_or_path: str) -> tuple[dict, Path]:
    p = Path(name_or_path)
    if not p.exists():
        p = PROFILES_DIR / f"{name_or_path}.json"
    if not p.exists():
        raise FlowError("profile", f"profile not found: {name_or_path} (looked in {PROFILES_DIR})")
    try:
        profile = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise FlowError("profile", f"could not read profile {p}: {e}") from e
    if not isinstance(profile, dict):
        raise FlowError("profile", f"profile {p}: expected a JSON object")
    for key, typ in PROFILE_SCHEMA.items():
        if key not in profile:
            raise FlowError("profile", f"profile {p}: missing key {key!r}")
        if not isinstance(profile[key], typ):
            want = " or ".join(t.__name__ for t in (typ if isinstance(typ, tuple) else (typ,)))
            raise FlowError("profile", f"profile {p}: {key!r} must be {want}")
    if profile["frontend"].get("name") not in FRONTENDS:
        raise FlowError("profile", f"profile {p}: frontend.name must be one of {FRONTENDS}")
    if not all_strings(profile["frontend"].get("args", [])):
        raise FlowError("profile", f"profile {p}: frontend.args must be a list of strings")
    for key in ("boolean_gates", "dff_types"):
        if not all_strings(profile[key]):
            raise FlowError("profile", f"profile {p}: {key!r} must be a list of strings")
    for key in SCRIPT_STAGES:
        cmds = profile["script"].get(key)
        if not all_strings(cmds):
            raise FlowError("profile", f"profile {p}: script.{key} must be a list of yosys command strings")
        if bad := bad_placeholders(cmds):
            raise FlowError("profile", f"profile {p}: script.{key} uses unknown placeholders {', '.join(bad)} "
                                       f"(allowed: {', '.join(SCRIPT_PLACEHOLDERS)})")
    lib = profile["liberty"]
    if not isinstance(lib.get("dir"), str) or not isinstance(lib.get("files"), list) or not lib["files"]:
        raise FlowError("profile", f"profile {p}: liberty.dir (string) and a non-empty liberty.files list are required")
    for entry in lib["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not isinstance(entry.get("sha256"), str):
            raise FlowError("profile", f"profile {p}: each liberty.files entry needs 'file' and 'sha256' strings")
    return profile, p.resolve()


def liberty_paths(profile: dict, verify: bool) -> list[Path]:
    lib = profile["liberty"]
    base = HERE / lib["dir"]
    paths = []
    for entry in lib["files"]:
        p = base / entry["file"]
        if not p.is_file():
            raise FlowError("profile", f"liberty file missing or not a regular file: {p}")
        if verify:
            try:
                got = sha256_file(p)
            except OSError as e:
                raise FlowError("profile", f"could not read liberty file {p}: {e}") from e
            if got != entry["sha256"]:
                raise FlowError("profile", f"liberty sha256 mismatch for {p.name}: expected {entry['sha256']}, got {got}")
        paths.append(p)
    return paths


def render(lines: list[str], **subst: str) -> list[str]:
    return [ln.format(**subst) for ln in lines]


def script_subst(profile: dict, top: str, libs: list[Path], out: dict[str, Path]) -> dict[str, str]:
    liberty_args = " ".join(f"-liberty {shlex.quote(str(p))}" for p in libs)
    return {
        "top": top,
        "abc_gates": profile["abc_gates"],
        "dfflegalize_cells": " ".join(f"-cell {t} 01" for t in profile["dff_types"]),
        "liberty_args": liberty_args,
        "word_json": shlex.quote(str(out["word_json"])),
        "generic_json": shlex.quote(str(out["generic_json"])),
        "mapped_json": shlex.quote(str(out["mapped_json"])),
        "netlist": shlex.quote(str(out["netlist"])),
        "stat_json": shlex.quote(str(out["stat_json"])),
        "stat_txt": shlex.quote(str(out["stat_txt"])),
    }


def word_script(profile: dict, read_cmd: str, top: str, libs: list[Path], out: dict[str, Path]) -> str:
    """Separate yosys run: the word-level checkpoint must not perturb the Boolean/mapped results."""
    subst = script_subst(profile, top, libs, out)
    return "\n".join([read_cmd, *render(profile["script"]["word"], **subst)]) + "\n"


def synth_script(profile: dict, read_cmd: str, top: str, libs: list[Path], out: dict[str, Path]) -> str:
    subst = script_subst(profile, top, libs, out)
    lines = [read_cmd, *render(profile["script"]["lower"], **subst), *render(profile["script"]["map"], **subst)]
    return "\n".join(lines) + "\n"


def rtl_vs_graph_setup(read_cmd: str, top: str, generic_json: Path) -> list[str]:
    """Load RTL as `gold` and the generic Boolean netlist as `gate` (both single-clock abstracted)."""
    return [
        read_cmd,
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        "memory_map",
        "opt_clean",
        "async2sync",
        "design -stash gold",
        f"read_json {shlex.quote(str(generic_json))}",
        f"hierarchy -top {top}",
        "async2sync",
        "design -stash gate",
        f"design -copy-from gold -as gold {top}",
        f"design -copy-from gate -as gate {top}",
    ]


def graph_vs_mapped_setup(top: str, generic_json: Path, mapped_json: Path, libs: list[Path]) -> list[str]:
    """Load the generic netlist as `gold` and the ASAP7 netlist (cells expanded from Liberty) as `gate`."""
    return [
        f"read_json {shlex.quote(str(generic_json))}",
        f"hierarchy -top {top}",
        "async2sync",
        "design -stash gold",
        f"read_json {shlex.quote(str(mapped_json))}",
        *[f"read_liberty {LIBERTY_READ_FLAGS} {shlex.quote(str(p))}" for p in libs],
        f"hierarchy -top {top}",
        "flatten",
        "opt_clean",
        "async2sync",
        "design -stash gate",
        f"design -copy-from gold -as gold {top}",
        f"design -copy-from gate -as gate {top}",
    ]


def equiv_induct_script(setup: list[str], seq: int) -> str:
    """Unbounded proof: pair outputs/registers by name, then SAT + k-induction over any state."""
    return "\n".join([
        *setup,
        "equiv_make gold gate equiv",
        "hierarchy -top equiv",
        f"equiv_simple -seq {seq}",
        f"equiv_induct -seq {seq}",
        "equiv_status -assert",
        "",
    ])


def equiv_bmc_script(setup: list[str], depth: int, resets: dict[str, bool]) -> str:
    """Bounded proof from reset: miter both designs and unroll `depth` cycles with SAT.

    Induction can fail on states that are unreachable from reset (e.g. a FIFO count above its
    depth) even though the designs agree on every reachable state; this covers that case with a
    bounded, reset-anchored proof over all input sequences of `depth` cycles.

    Initial state is undefined (x) in both designs, the declared resets are asserted in cycle 1
    and outputs are compared from cycle 2 on. Only registers the reset actually initialises
    become defined; everything else stays x until written, and an x on a gold output means the
    RTL leaves that value unspecified, so it is not compared (`-ignore_gold_x`). A defined gold
    output must be matched by a defined, equal gate output. Forcing every register to zero
    would instead prove nothing about legal non-zero power-up states."""
    if depth < 2:
        raise ValueError(f"bounded proof depth must be >= 2 (cycle 1 is the reset cycle), got {depth}")
    set_at = " ".join(f"-set-at 1 in_{r} {0 if low else 1}" for r, low in resets.items())
    return "\n".join([
        *setup,
        "miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter",
        "hierarchy -top miter",
        f"sat -verify -prove trigger 0 -seq {depth} -prove-skip 1 -set-init-undef -enable_undef -set-def-inputs"
        f" {set_at} miter".replace("  ", " "),
        "",
    ])


ABC_HELP_RE = re.compile(r'instead of "([^"]+)" to execute ABC')
ABC_BUILTIN = "<yosys-bindir>/yosys-abc"


@functools.cache
def abc_default(yosys: str) -> str | None:
    """What this yosys's own `help abc` says the `-exe` default is: the literal `<yosys-bindir>/yosys-abc`
    for a build with the bundled ABC, or the compiled-in path of a build configured with an external ABC
    (ABCEXTERNAL). None when yosys cannot be run or the text is not recognised."""
    try:
        out = subprocess.run([yosys, "-Q", "-T", "-p", "help abc"], capture_output=True, text=True, timeout=60,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = ABC_HELP_RE.search(out.stdout)
    return m.group(1) if m else None


def abc_executable(yosys: str) -> str | None:
    """The ABC binary this yosys runs (init_abc_executable_name in kernel/yosys.cc): `yosys-abc` next to
    the resolved yosys executable (it is found from /proc/self/exe, i.e. after following symlinks) for a
    build with the bundled ABC; for a build with an external ABC, `$ABC` when set, else the compiled-in
    path. None if yosys will not say (cannot be run, unrecognised help text)."""
    default = abc_default(yosys)
    if default is None:
        return None
    if default == ABC_BUILTIN:
        return str(Path(yosys).resolve().with_name("yosys-abc"))
    return os.environ.get("ABC") or default


def run_yosys(yosys: str, script: str, script_path: Path, log_path: Path, timeout: int) -> tuple[bool, str, float]:
    script_path.write_text(script)
    if log_path.exists():
        log_path.unlink()
    t0 = time.time()
    try:
        proc = subprocess.run(
            [yosys, "-q", "-T", "-l", str(log_path), "-s", str(script_path)],
            capture_output=True, text=True, timeout=timeout, check=False, cwd=script_path.parent,
        )
        ok, extra = proc.returncode == 0, proc.stderr
    except subprocess.TimeoutExpired:
        ok, extra = False, f"yosys timed out after {timeout}s"
    except OSError as e:
        ok, extra = False, f"ERROR: cannot run {yosys}: {e.strerror or e}"
    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    return ok, log_text + ("\n" + extra if extra else ""), round(time.time() - t0, 3)


def first_error(log_text: str, fallback: str) -> str:
    _, errors = extract_yosys_diagnostics(log_text)
    return errors[0] if errors else fallback


def parse_reset_args(values: list[str] | None) -> dict[str, bool] | None:
    """['rst_ni:low', 'soft_rst'] -> {'rst_ni': True, 'soft_rst': False}; raises ValueError on any other form."""
    if values is None:
        return None
    out: dict[str, bool] = {}
    for v in values:
        name, sep, pol = v.partition(":")
        if not name or (sep and pol != "low"):
            raise ValueError(f"--sim-reset {v!r}: expected PORT or PORT:low")
        out[name] = pol == "low"
    return out


def flat_summary(boolean: dict, mapped: dict) -> dict:
    """Flat one-level metrics (`and`, `xor`, ..., `mapped_cell_area`) for quick diffs between runs."""
    s = {k: boolean[k] for k in ("not", "and", "nand", "or", "nor", "xor", "xnor", "mux", "dff", "gate_total")}
    s["mapped_cell_total"] = mapped["num_cells"]
    s["mapped_cells_by_type"] = mapped["cells_by_type"]
    s["edge_total"] = boolean["edge_total"]
    s["max_depth"] = boolean["max_depth"]
    s["max_fanout"] = boolean["max_fanout"]
    s["avg_fanout"] = boolean["avg_fanout"]
    s["mapped_cell_area"] = mapped["area"]
    s["area_unit"] = mapped["area_unit"]
    return s


def parse_equiv_status(log_text: str) -> dict:
    """`equiv_status` prints 'Found N $equiv cells ... Of those cells P are proven and U are unproven.'"""
    found = re.search(r"Found (\d+) \$equiv cells", log_text)
    proven = re.search(r"Of those cells (\d+) are proven and (\d+) are unproven", log_text)
    return {
        "equiv_cells": int(found.group(1)) if found else None,
        "proven": int(proven.group(1)) if proven else None,
        "unproven": int(proven.group(2)) if proven else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="SystemVerilog / Verilog sources (packages first)")
    ap.add_argument("--top", required=True, help="top module name (always explicit; never inferred)")
    ap.add_argument("-o", "--out-dir", required=True, help="artifact directory (created)")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help=f"profile name or path (default: {DEFAULT_PROFILE})")
    ap.add_argument("-I", "--include", action="append", default=[], help="include directory")
    ap.add_argument("-D", "--define", action="append", default=[], help="preprocessor define NAME[=VALUE]")
    ap.add_argument("--frontend", choices=FRONTENDS, help="override the profile's frontend (recorded in metrics)")
    ap.add_argument("--slang-arg", action="append", default=[], metavar="ARG", help="extra read_slang option")
    ap.add_argument("--no-equiv", action="store_true", help="skip the formal equivalence checks")
    ap.add_argument("--equiv-seq", type=int, default=5, help="induction / unrolling depth for equiv passes")
    ap.add_argument("--equiv-bmc", type=int, default=10,
                    help="cycles for the bounded-from-reset fallback proof when induction fails (>= 2: cycle 1 is "
                         "the reset cycle and is not compared); 0 disables")
    ap.add_argument("--sim-cycles", type=int, default=200,
                    help="random differential simulation RTL vs mapped netlist (iverilog); 0 disables")
    ap.add_argument("--seed", type=int, default=1, help="stimulus seed for --sim-cycles")
    ap.add_argument("--sim-clock", action="append", default=None, metavar="PORT",
                    help="clock input for the simulation testbench (default: guessed from names)")
    ap.add_argument("--sim-reset", action="append", default=None, metavar="PORT[:low]",
                    help="reset input for the simulation testbench, ':low' for active-low (default: guessed)")
    ap.add_argument("--no-verify-libs", action="store_true", help="skip Liberty SHA-256 verification")
    ap.add_argument("--yosys", help="yosys binary (default: $YOSYS, ./build/yosys, PATH)")
    ap.add_argument("--sv2v", help="sv2v binary for --frontend sv2v")
    ap.add_argument("--timeout", type=int, default=3600, help="per-yosys-invocation timeout (s)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    if not IDENT_RE.match(args.top):
        ap.error(f"--top must be a plain module identifier, got {args.top!r}")
    if args.equiv_bmc < 0 or args.equiv_bmc == 1:
        ap.error("--equiv-bmc must be 0 (disabled) or at least 2: cycle 1 is the reset cycle and is skipped, "
                 "so a depth of 1 would compare no outputs")

    t_start = time.time()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {k: out_dir / v for k, v in ARTIFACTS.items()}
    purge_outputs(out_dir)
    sources = [str(Path(s).resolve()) for s in args.sources]

    metrics: dict = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "status": "failed",
        "stage": None,
        "top": args.top,
        "summary": None,
        "sources": [{"path": s, "sha256": None} for s in sources],
        "profile": None,
        "tools": {},
        "frontend": None,
        "boolean": None,
        "mapped": None,
        "equivalence": None,
        "simulation": None,
        "artifacts": {k: str(v) for k, v in out.items() if k not in ("stat_json", "stat_txt", "script")},
        "timing": {},
        "warnings": [],
        "errors": [],
        "wall_seconds": None,
    }

    schema_versions = {"word_level": WORD_SCHEMA_VERSION, "sequential_overlay": SEQ_SCHEMA_VERSION,
                       "boolean_graph": GRAPH_SCHEMA_VERSION, "mapped_cells": MAPPED_SCHEMA_VERSION,
                       "metrics": METRICS_SCHEMA_VERSION}

    def write_reports() -> bool:
        """metrics.json, then summary.md, then the manifest (it hashes the other two). A report that
        cannot be written marks the run failed/artifacts and the others are still attempted."""
        out["metrics"].write_text(json.dumps(metrics, indent=2) + "\n")
        ok = True
        for key, render in (("summary", lambda: render_summary(metrics)),
                            ("manifest", lambda: json.dumps(build_manifest(
                                metrics, out, sys.argv if argv is None else [sys.argv[0], *argv], schema_versions),
                                indent=1) + "\n")):
            try:
                out[key].write_text(render())
            except OSError as e:
                metrics["status"], metrics["stage"] = "failed", "artifacts"
                msg = f"could not write {ARTIFACTS[key]}: {e}"
                if msg not in metrics["errors"]:
                    metrics["errors"].append(msg)
                ok = False
        return ok

    def finish(code: int) -> int:
        metrics["wall_seconds"] = round(time.time() - t_start, 3)
        if not write_reports():
            code = code or 1
            write_reports()  # second pass: every report that can still be written now says failed/artifacts
        if not args.quiet:
            if metrics["status"] == "ok":
                b, m = metrics["boolean"], metrics["mapped"]
                eq = metrics["equivalence"]
                eq_s = "" if eq is None else f" equiv={'proven' if eq['status'] == 'proven' else eq['status']}"
                print(
                    f"[bool_area] OK top={args.top} gates={b['gate_total']} dffs={b['dff']} depth={b['max_depth']} "
                    f"cells={m['num_cells']} area={m['area']}{eq_s} ({metrics['wall_seconds']}s) -> {out_dir}"
                )
            else:
                first = metrics["errors"][0] if metrics["errors"] else "see metrics.json"
                print(f"[bool_area] FAILED[{metrics['stage']}] top={args.top}: {first} -> {out_dir}", file=sys.stderr)
        return code

    def fail(stage: str, msg: str, code: int = 1) -> int:
        metrics["stage"] = stage
        metrics["errors"].append(msg)
        return finish(code)

    try:
        profile, profile_path = load_profile(args.profile)
        libs = liberty_paths(profile, verify=not args.no_verify_libs)
        profile_sha = sha256_file(profile_path)
        lib_hashes = [{"file": p.name, "sha256": sha256_file(p)} for p in libs]
    except FlowError as e:
        return fail(e.stage, str(e), 2)
    except OSError as e:
        return fail("profile", f"could not read profile or liberty file: {e}", 2)
    metrics["profile"] = {
        "name": profile["name"],
        "version": profile["version"],
        "path": str(profile_path),
        "sha256": profile_sha,
        "liberty": lib_hashes,
        "liberty_verified": not args.no_verify_libs,
        "boolean_gates": profile["boolean_gates"],
        "dff_types": profile["dff_types"],
    }
    for entry in metrics["sources"]:
        source_path = Path(entry["path"])
        if not source_path.is_file():
            return fail("inputs", f"source not found: {entry['path']}", 2)
        try:
            entry["sha256"] = sha256_file(source_path)
        except OSError as e:
            return fail("inputs", f"could not read source {entry['path']}: {e}", 2)
    try:
        sim_reset_args = parse_reset_args(args.sim_reset)
    except ValueError as e:
        return fail("inputs", str(e), 2)

    # shutil.which on a path checks that it is an executable regular file, not just that it exists
    yosys = find_yosys(args.yosys)
    if not yosys or not shutil.which(yosys):
        return fail("tools", "yosys binary not found or not executable (build the repo, set $YOSYS, or pass --yosys)", 2)
    metrics["tools"] = {"yosys": yosys, "yosys_version": tool_version(yosys, "-V"), "abc": abc_executable(yosys)}
    frontend = args.frontend or profile["frontend"]["name"]
    sv2v = find_sv2v(args.sv2v)
    if sv2v and not shutil.which(sv2v):
        # sv2v is only required by --frontend sv2v (the simulation falls back to reading the RTL directly);
        # an explicit --sv2v or a frontend that needs it must not be silently ignored
        if args.sv2v or frontend == "sv2v":
            return fail("tools", f"sv2v not executable: {sv2v}", 2)
        sv2v = None
    metrics["tools"]["sv2v"] = sv2v
    if sv2v:
        metrics["tools"]["sv2v_version"] = tool_version(sv2v, "--version")

    includes = [str(Path(i).resolve()) for i in args.include]
    metrics["frontend"] = {"name": frontend, "slang_args": None, "include_dirs": includes, "defines": list(args.define)}
    read_sources = sources
    if frontend == "sv2v":
        if not sv2v:
            return fail("tools", "sv2v not found (set $SV2V or pass --sv2v)", 2)
        conv = out_dir / SV2V_OUT
        cmd = [sv2v, *[f"-I{i}" for i in includes], *[f"-D{d}" for d in args.define], f"--top={args.top}",
               "-w", str(conv), *sources]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout, check=False)
        except subprocess.TimeoutExpired:
            return fail("parse", f"sv2v timed out after {args.timeout}s")
        except OSError as e:
            return fail("tools", f"cannot run sv2v: {e}", 2)
        if proc.returncode != 0:
            return fail("parse", f"sv2v failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.returncode}")
        read_sources, includes, defines = [str(conv)], [], []
    else:
        defines = list(args.define)
    slang_args = None
    if frontend == "slang":
        slang_args = list(profile["frontend"].get("args", SLANG_SYNTH_DEFAULTS)) + args.slang_arg
        metrics["frontend"]["slang_args"] = slang_args
    read_cmd = read_cmd_for(frontend, read_sources, includes, defines, args.top, slang_args)

    # --- 1a. word-level checkpoint: RTL -> proc/flatten/opt_dff -> write_json (own yosys run) ---
    for stale in ("word_json", "generic_json", "mapped_json", "netlist", "stat_json", "stat_txt"):
        if out[stale].exists():
            out[stale].unlink()
    ok, log_text, secs = run_yosys(yosys, word_script(profile, read_cmd, args.top, libs, out), out["word_script"],
                                   out["word_log"], args.timeout)
    metrics["timing"]["word_seconds"] = secs
    warnings, _ = extract_yosys_diagnostics(log_text)
    metrics["warnings"].extend(warnings)
    if not ok or not out["word_json"].exists():
        stage = "parse" if "Executing PROC pass" not in log_text else "word"
        return fail(stage, first_error(log_text, "yosys failed during word-level elaboration (see word.log)"))

    # --- 1b. synthesis: RTL -> generic gates -> ASAP7 (single yosys run) ---
    ok, log_text, secs = run_yosys(yosys, synth_script(profile, read_cmd, args.top, libs, out), out["script"],
                                   out["log"], args.timeout)
    metrics["timing"]["synth_seconds"] = secs
    warnings, _ = extract_yosys_diagnostics(log_text)
    metrics["warnings"].extend(w for w in warnings if w not in metrics["warnings"])
    if not ok or not out["generic_json"].exists():
        stage = "parse" if not out["generic_json"].exists() and "write_json" not in log_text else "lowering"
        if "Executing Liberty frontend" in log_text or "DFFLIBMAP" in log_text:
            stage = "mapping"
        return fail(stage, first_error(log_text, "yosys failed during synthesis (see yosys.log)"))
    if not out["mapped_json"].exists() or not out["stat_json"].exists():
        return fail("mapping", first_error(log_text, "mapping did not produce mapped_yosys.json / stat.json"))

    # --- 2a. word-level operations (before any bit-level mapping) ---
    try:
        word_data = json.loads(out["word_json"].read_text())
        word = build_report(word_data, args.top, src_base=out_dir)
        out["word"].write_text(json.dumps(word, indent=1) + "\n")
        seq = build_overlay(word_data, args.top, src_base=out_dir)
        out["sequential"].write_text(json.dumps(seq, indent=1) + "\n")
    except UnsupportedCell as e:
        return fail("word", str(e))
    except (ValueError, KeyError) as e:
        return fail("word", f"could not build word-level report: {e}")
    except OSError as e:
        return fail("artifacts", f"could not write word-level / sequential report: {e}")
    metrics["word_level"] = {"schema_version": WORD_SCHEMA_VERSION, **word["summary"]}
    metrics["sequential"] = {"schema_version": SEQ_SCHEMA_VERSION, **seq["summary"]}

    # --- 2b. Boolean graph + structural metrics ---
    try:
        graph = build_graph(json.loads(out["generic_json"].read_text()), args.top, set(profile["dff_types"]))
        out["graph"].write_text(json.dumps(graph, indent=1) + "\n")
        metrics["boolean"] = compute_metrics(graph)
        metrics["boolean"]["graph_schema_version"] = GRAPH_SCHEMA_VERSION
    except UnsupportedCell as e:
        return fail("graph", str(e))
    except (ValueError, KeyError) as e:
        return fail("graph", f"could not build Boolean graph: {e}")
    if metrics["boolean"]["combinational_loop"]:
        return fail("graph", "combinational loop in Boolean graph (depth undefined)")
    for name in [*(args.sim_clock or []), *(sim_reset_args or {})]:
        port = graph["ports"].get(name)
        if port is None or port["direction"] != "input":
            return fail("inputs", f"--sim-clock/--sim-reset {name}: not an input port of {args.top} "
                                  f"(inputs: {sorted(p for p, d in graph['ports'].items() if d['direction'] == 'input')})", 2)

    # --- 3. mapped cell counts + Liberty area ---
    try:
        mapped = summarize_stats(out["stat_json"], args.top, str(libs[0]), out["stat_txt"])
    except (ValueError, KeyError) as e:
        return fail("mapping", f"could not parse stat output: {e}")
    mapped["area_unit"] = profile["liberty"].get("area_unit", "liberty area units")
    if mapped.get("area_is_lower_bound"):
        return fail("mapping", f"mapped cells without Liberty area: {sorted(mapped['unknown_area_cell_types'])}")
    metrics["mapped"] = mapped
    metrics["summary"] = flat_summary(metrics["boolean"], mapped)

    # --- 3b. mapped cells with pin-to-net connections, cross-checked against `stat` ---
    try:
        mapped_data = json.loads(out["mapped_json"].read_text())
        cells = build_cells(mapped_data, args.top, load_liberty(libs, cell_types(mapped_data, args.top)), src_base=out_dir)
        out["mapped_cells"].write_text(json.dumps(cells, indent=1) + "\n")
    except UnsupportedCell as e:
        return fail("mapping", str(e))
    except (ValueError, KeyError) as e:
        return fail("mapping", f"could not build mapped-cell report: {e}")
    except OSError as e:
        return fail("artifacts", f"could not write mapped-cell report: {e}")
    cs = cells["summary"]
    if cs["cells"] != mapped["num_cells"] or abs(cs["area"] - mapped["area"]) > 1e-6 * max(1, cs["cells"]):
        return fail("mapping", f"mapped_cells.json disagrees with stat: {cs['cells']} cells / area {cs['area']} "
                               f"vs {mapped['num_cells']} / {mapped['area']}")
    mapped["cells_schema_version"] = MAPPED_SCHEMA_VERSION
    mapped["pin_connections"] = cs["pins"]

    # --- 4. formal equivalence: RTL == Boolean graph == mapped netlist ---
    if args.no_equiv:
        metrics["equivalence"] = None
    else:
        setups = {
            "rtl_vs_graph": rtl_vs_graph_setup(read_cmd, args.top, out["generic_json"]),
            "graph_vs_mapped": graph_vs_mapped_setup(args.top, out["generic_json"], out["mapped_json"], libs),
        }
        try:
            _, sim_resets, _, _ = classify_ports(graph["ports"], args.sim_clock, sim_reset_args)
        except ValueError as e:  # inferred classification only (e.g. inout ports); explicit ports were validated above
            sim_resets = dict(sim_reset_args or {})
            metrics["warnings"].append(f"bounded-proof reset classification unavailable: {e}")
        eq: dict = {"status": "proven",
                    "method": f"yosys equiv_make + equiv_simple/equiv_induct -seq {args.equiv_seq}; "
                              f"fallback: miter + sat -seq {args.equiv_bmc} from reset",
                    "checks": {}}
        for name, setup in setups.items():
            ok, log_text, secs = run_yosys(yosys, equiv_induct_script(setup, args.equiv_seq), out_dir / f"equiv_{name}.ys",
                                           out_dir / f"equiv_{name}.log", args.timeout)
            status = parse_equiv_status(log_text)
            status["seconds"] = secs
            status["log"] = str(out_dir / f"equiv_{name}.log")
            status["status"] = "proven" if ok and status["unproven"] == 0 else "failed"
            if not ok and status["unproven"] is None:
                status["error"] = first_error(log_text, "equivalence run failed (see log)")
            # equiv_make pairs every output bit (plus same-named registers); fewer $equiv cells
            # than output bits means the two designs were not actually compared
            if status["status"] == "proven" and (status["equiv_cells"] or 0) < metrics["boolean"]["output_bits"]:
                status["status"] = "failed"
                status["error"] = (f"only {status['equiv_cells']} $equiv cells for "
                                   f"{metrics['boolean']['output_bits']} output bits")
            if status["status"] == "failed" and status["unproven"] and args.equiv_bmc > 0 and not sim_resets:
                # nothing to anchor the bounded proof on: an all-x start would compare nothing
                status["error"] = (f"{status['unproven']} cells not provable by induction and no reset port is known "
                                   "for the bounded fallback (pass --sim-reset PORT[:low])")
            elif status["status"] == "failed" and status["unproven"] and args.equiv_bmc > 0:
                # induction left cells unproven: fall back to a bounded proof anchored at reset
                ok, log_text, bmc_secs = run_yosys(yosys, equiv_bmc_script(setup, args.equiv_bmc, sim_resets),
                                                   out_dir / f"equiv_{name}_bmc.ys", out_dir / f"equiv_{name}_bmc.log",
                                                   args.timeout)
                status["bmc"] = {"depth": args.equiv_bmc, "resets": sorted(sim_resets), "seconds": bmc_secs,
                                 "log": str(out_dir / f"equiv_{name}_bmc.log"),
                                 "status": "proven" if ok and "SUCCESS" in log_text else "failed"}
                secs += bmc_secs
                if status["bmc"]["status"] == "proven":
                    status["status"] = "bounded"
                    metrics["warnings"].append(
                        f"equivalence {name}: {status['unproven']} cells not provable by induction; "
                        f"bounded proof over {args.equiv_bmc} cycles from reset instead")
                else:
                    status["error"] = first_error(log_text, "bounded model check found a mismatch or failed (see log)")
            eq["checks"][name] = status
            metrics["timing"][f"equiv_{name}_seconds"] = secs
            if status["status"] == "failed":
                eq["status"] = "failed"
            elif status["status"] == "bounded" and eq["status"] == "proven":
                eq["status"] = "bounded"
        metrics["equivalence"] = eq
        if eq["status"] == "failed":
            bad = [n for n, c in eq["checks"].items() if c["status"] == "failed"]
            return fail("equivalence", f"equivalence not proven: {', '.join(bad)} (see equiv_*.log)")

    # --- 5. random differential simulation RTL vs mapped netlist (sample, not proof) ---
    if args.sim_cycles > 0:
        try:  # port classification is decided by the design, not by which simulators this machine has
            classify_ports(graph["ports"], args.sim_clock, sim_reset_args)
        except ValueError as e:
            return fail("simulation", str(e))
        iverilog, vvp = shutil.which("iverilog"), shutil.which("vvp")
        metrics["tools"].update(iverilog=iverilog, vvp=vvp)
        if not (iverilog and vvp):
            metrics["simulation"] = {"status": "skipped", "error": "iverilog/vvp not on PATH"}
            metrics["warnings"].append("simulation skipped: iverilog/vvp not found")
        else:
            t0 = time.time()
            sim = run_diff_sim(
                top=args.top, ports=graph["ports"], rtl_sources=sources, includes=[str(Path(i).resolve()) for i in args.include],
                defines=list(args.define), mapped_json=out["mapped_json"], libs=libs, yosys=yosys, sv2v=sv2v,
                iverilog=iverilog, vvp=vvp, work=out_dir / SIM_DIR, cycles=args.sim_cycles, seed=args.seed,
                timeout=args.timeout, clocks=args.sim_clock, resets=sim_reset_args,
            )
            sim["method"] = "iverilog random differential simulation, RTL (via sv2v) vs mapped netlist + Liberty-derived cell models"
            metrics["timing"]["sim_seconds"] = round(time.time() - t0, 3)
            metrics["simulation"] = sim
            if sim.get("gate_x_bits"):
                metrics["warnings"].append(f"simulation: {sim['gate_x_bits']} gate-level X bits where RTL was known (X-pessimism)")
            if sim["status"] != "match":
                return fail("simulation", f"RTL vs mapped simulation {sim['status']}: {sim.get('error') or sim.get('first_mismatches', [''])[0]}")

    metrics["status"] = "ok"
    metrics["stage"] = "done"
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
