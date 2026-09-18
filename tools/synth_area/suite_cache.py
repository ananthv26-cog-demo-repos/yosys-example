"""
suite_cache: skip a corpus block whose previous run is provably still valid.

After a block finishes with `status: ok`, run_suite.py drops `suite_cache.json` into the block's
output directory. It records the sha256 of everything the run depended on and of everything it
produced:

    argv            the exact bool_area.py command line (sources, top, defines, includes, extra args)
    inputs          every source file, every file they `include (transitively, resolved next to the
                    including file and then in the -I directories, as the frontends do), the profile,
                    its Liberty files and every file its yosys commands name (`techmap -map x.v`, a
                    relative one from the block's output directory, which is where yosys runs)
    include_dirs    every file under every -I directory
    tools           the python interpreter, yosys (slang is linked in), the abc binary yosys runs (its
                    sibling yosys-abc, or for a build with an external ABC that path unless $ABC is set,
                    as yosys's own `help abc` reports), sv2v, iverilog and vvp, each resolved the way
                    bool_area.py resolves it today (argv, $YOSYS/$SV2V/$ABC, ./build, PATH), so pointing
                    the environment at another binary is a miss
    yosys_share     every file in the share/ directory that yosys loads its `+/` support files from
                    (techmap.v, simcells.v, ...), since `synth` reads them at run time; asked of yosys
                    itself, so it is the directory this build really uses
    code            every tools/synth_area/*.py module
    artifacts       every path the flow owns (bool_area.owned_files): the fixed artifacts, sv2v output,
                    equiv_* scripts/logs and the whole sim/ directory; absent ones are recorded as absent

On the next run the block is reused only if every one of those hashes is unchanged and every
artifact is still on disk byte-for-byte (and nothing that was absent has appeared). Anything else (a
missing tool, an unreadable file, an older record) is a miss, never a guess. Failed runs are not
recorded: a block that failed reruns. Neither is a run during which an input changed: the inputs and
tools are fingerprinted once before the block is launched and once after it has finished, and the
record is written only if the two agree, so an edit that lands mid-run (whichever contents yosys
happened to read) leaves no record and the block reruns next time.

That is the whole dependency boundary: files named by the sources, the profile or the tool set are
hashed; a dependency the text does not name (`include `MACRO, a path a yosys command computes at run
time, a file a plugin opens on its own) cannot be, so wherever one is detected the block is recorded
as not cacheable and always reruns. Anything yosys reads that is neither of those (environment
variables other than the tool selectors, the system time) is outside it by design.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from bool_area import (
    ARTIFACTS,
    DEFAULT_PROFILE,
    SCRIPT_STAGES,
    FlowError,
    abc_executable,
    liberty_paths,
    load_profile,
    owned_files,
)
from run_report import sha256_file
from synth_area import find_sv2v, find_yosys

CACHE_SCHEMA_VERSION = 2
RECORD = "suite_cache.json"
HERE = Path(__file__).resolve().parent
INCLUDE_RE = re.compile(r'^[ \t]*`include[ \t]+(?:"([^"\n]*)"|<([^>\n]*)>|(\S+))', re.MULTILINE)
FILE_TOKEN_RE = re.compile(r"/|\.(v|sv|vh|svh|lib|ys|json|il|blif|aig|lut|txt|tcl)$", re.IGNORECASE)
SHARE_PROBE = "__suite_cache_share_probe__.v"
SHARE_RE = re.compile(r"`(/[^`'\n]*)/" + re.escape(SHARE_PROBE) + "'")  # not the `+/...' command echo


def try_sha256(path: Path) -> str | None:
    try:
        return sha256_file(path) if path.is_file() else None
    except (OSError, ValueError):
        return None


@functools.cache
def tool_sha256(path: str) -> str | None:
    """Tool binaries are shared by every block of a run and large (yosys is ~40 MB): hash once."""
    return try_sha256(Path(path))


def dir_hashes(root: Path) -> dict[str, str | None]:
    """{relative path: sha256} for every file under `root` (an include directory), keyed by the path the
    frontend would use. Directory symlinks are followed (the frontend does), each target once."""
    try:
        if not root.is_dir():
            return {"": None}
    except (OSError, ValueError):
        return {"": None}
    hashes: dict[str, str | None] = {}
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath, name)
            hashes[str(p.relative_to(root))] = try_sha256(p)
    return hashes


def include_files(sources: list[str], include_dirs: list[str]) -> dict[str, str | None]:
    """{path: sha256} of every file the sources `include, transitively, each resolved the way slang and
    sv2v resolve it: next to the including file first, then the -I directories in order. Every `include
    line counts, also ones in an inactive `ifdef branch (conservative). One that resolves nowhere, or
    whose file name is not literal (`include `HEADER), is recorded under the including file as None,
    which keeps the block from being cached at all."""
    found: dict[str, str | None] = {}
    todo, seen = list(sources), set()
    while todo:
        src = todo.pop()
        if src in seen:
            continue
        seen.add(src)
        try:
            text = Path(src).read_text(errors="replace")
        except OSError:
            continue  # hashes as None wherever it is recorded, so the record is incomplete anyway
        for quoted, angled, other in INCLUDE_RE.findall(text):
            name = quoted or angled
            if not name:
                found[f"{src}: `include {other}"] = None
                continue
            for d in (Path(src).parent, *map(Path, include_dirs)):
                if (d / name).is_file():
                    p = str(d / name)
                    found[p] = try_sha256(Path(p))
                    todo.append(p)
                    break
            else:
                found[f"{src}: `include {name}"] = None
    return found


@functools.cache
def yosys_share_dir(yosys: str) -> Path | None:
    """Where this yosys expands `+/` to, asked of yosys itself: reading a `+/` file that does not exist
    makes it print the full path it tried, i.e. whichever of share/ beside the binary, ../share/<prefix>yosys/,
    the compiled-in data directory or the pyosys one this build picked (init_share_dirname in
    kernel/yosys.cc). None when yosys cannot be run or does not name a directory, which keeps the block
    from being cached."""
    try:
        out = subprocess.run([yosys, "-Q", "-T", "-p", f"read_verilog +/{SHARE_PROBE}"], capture_output=True,
                             text=True, timeout=60, check=False)
        m = SHARE_RE.search(out.stdout + out.stderr)
        return Path(m.group(1)) if m and Path(m.group(1)).is_dir() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


@functools.cache
def share_hashes(yosys: str) -> dict[str, str | None]:
    """dir_hashes of yosys_share_dir(yosys) (a few hundred files, shared by every block: hash once)."""
    share = yosys_share_dir(yosys)
    return dir_hashes(share) if share else {"": None}


def code_hashes() -> dict[str, str | None]:
    return {p.name: try_sha256(p) for p in sorted(HERE.glob("*.py"))}


def options(argv: list[str], *names: str) -> list[str]:
    """Every value of `--name X` / `--name=X` (any of `names`) in a bool_area.py argument list, in order."""
    values = []
    for i, a in enumerate(argv):
        for name in names:
            if a == name and i + 1 < len(argv):
                values.append(argv[i + 1])
            elif a.startswith(name + "="):
                values.append(a[len(name) + 1:])
    return values


def option(argv: list[str], *names: str) -> str | None:
    """The last such value (as argparse takes it), or None."""
    values = options(argv, *names)
    return values[-1] if values else None


def out_dir(argv: list[str]) -> Path | None:
    """The block's output directory as bool_area.py resolves `-o`: the working directory of every yosys
    run, hence what a relative path in a profile command is relative to."""
    value = option(argv, "-o", "--out-dir")
    return Path(value).resolve() if value else None


def planned_manifest(sources: list[str], argv: list[str]) -> dict:
    """What run_manifest.json will say about the inputs of the run `argv` is about to make (the resolved
    sources, the -I directories resolved, the profile file as bool_area.py finds it), so the inputs can
    be fingerprinted before the block starts and compared with what it reports afterwards."""
    try:
        profile: str | None = str(load_profile(option(argv, "--profile") or DEFAULT_PROFILE)[1])
        includes = [str(Path(i).resolve()) for i in options(argv, "-I", "--include")]
    except (FlowError, OSError, ValueError):  # the run will fail on this too; nothing to compare against
        profile, includes = None, []
    return {"sources": [{"path": s} for s in sources], "profile": {"path": profile},
            "frontend": {"include_dirs": includes}}


def executable(tool: str | None) -> str | None:
    """The file a bare name or path runs today (executable regular file, PATH lookup for bare names)."""
    try:
        return shutil.which(tool) if tool else None
    except (OSError, ValueError):
        return None


def tool_paths(argv: list[str], python: str) -> dict[str, str | None]:
    """The binaries a bool_area.py run with `argv` would use *now*, resolved exactly as bool_area.py
    resolves them (explicit option, then $YOSYS/$SV2V/$ABC, ./build/yosys, PATH). Comparing today's
    resolution with the recorded one catches both a replaced file at the same path and the environment
    or PATH selecting a different one; a tool that has vanished or appeared is a miss too."""
    yosys = executable(find_yosys(option(argv, "--yosys")))
    return {
        "python": executable(python),
        "yosys": yosys,
        "abc": executable(abc_executable(yosys)) if yosys else None,
        "sv2v": executable(find_sv2v(option(argv, "--sv2v"))),
        "iverilog": executable("iverilog"),
        "vvp": executable("vvp"),
    }


def profile_inputs(profile_path: object, cwd: Path | None) -> dict[str, str | None]:
    """{path: sha256} of the profile, the Liberty files it names and the files its yosys commands name;
    {'': None} (never complete) if it cannot be loaded."""
    if not isinstance(profile_path, str):
        return {"": None}
    try:
        profile, _ = load_profile(profile_path)
        libs = liberty_paths(profile, verify=False)
    except (FlowError, OSError, ValueError):
        return {"": None}
    return {**{p: try_sha256(Path(p)) for p in [profile_path, *map(str, libs)]}, **script_files(profile, cwd)}


def script_files(profile: dict, cwd: Path | None) -> dict[str, str | None]:
    """{path: sha256} of every file the profile's yosys commands name themselves (`techmap -map x.v`,
    `read_liberty y.lib`, `script z.ys`): any word with a directory separator or a file extension that
    is not an option or a `{placeholder}` (those are flow-owned outputs). `+/...` is yosys's own share/
    directory, hashed as yosys_share. An absolute path is hashed as it is; a relative one is looked up
    from `cwd`, the directory bool_area.py runs yosys in (its output directory); one that is not a file
    there, or any relative one when `cwd` is unknown, is None, which keeps every block using the profile
    from being cached (the command may compute the path at run time)."""
    found: dict[str, str | None] = {}
    for stage in SCRIPT_STAGES:
        for cmd in profile["script"][stage]:
            for word in cmd.split():
                word = word.strip("\"'")
                if word.startswith(("-", "+/")) or "{" in word or not FILE_TOKEN_RE.search(word):
                    continue
                if Path(word).is_absolute():
                    found[word] = try_sha256(Path(word))
                else:
                    found[word] = try_sha256(cwd / word) if cwd else None
    return found


def section(container: dict, key: str) -> dict:
    v = container.get(key)
    return v if isinstance(v, dict) else {}


def fingerprint(argv: list[str], python: str, manifest: dict) -> dict | None:
    """Hashes of everything a bool_area.py run with `argv` depends on. The manifest of the run being
    recorded (or reused) says which input files those were (sources, include dirs, profile); the tool
    binaries are whatever `argv` and the environment select today. None if a path in there cannot even
    be looked at (a NUL byte, a permission error): the caller treats that as a miss."""
    try:
        return fingerprint_or_raise(argv, python, manifest)
    except (OSError, ValueError):
        return None


def fingerprint_or_raise(argv: list[str], python: str, manifest: dict) -> dict:
    profile = section(manifest, "profile").get("path")
    entries = manifest.get("sources")
    sources = [str(s.get("path")) for s in entries if isinstance(s, dict)] if isinstance(entries, list) else []
    sources = sources or [""]
    includes = section(manifest, "frontend").get("include_dirs")
    includes = [str(d) for d in includes] if isinstance(includes, list) else []
    tools = tool_paths(argv, python)
    return {
        "argv": list(argv),
        "python": python,
        "inputs": {**{p: try_sha256(Path(p)) for p in sources}, **profile_inputs(profile, out_dir(argv)),
                   **include_files(sources, includes)},
        "include_dirs": {d: dir_hashes(Path(d)) for d in includes},
        "tools": {name: tool_sha256(path) if path else None for name, path in tools.items()},
        "yosys_share": share_hashes(tools["yosys"]) if tools["yosys"] else {"": None},
        "code": code_hashes(),
    }


def artifact_hashes(block_out: Path) -> dict[str, str | None]:
    """{path relative to block_out: sha256} for every flow-owned path; None for one not on disk. Files
    found by glob (equiv_*, sim/) only appear while they exist, so deleting one changes the map."""
    return {str(p.relative_to(block_out)): try_sha256(p) for p in owned_files(block_out)}


def record(block_out: Path, argv: list[str], python: str, before: dict | None) -> bool:
    """Write the cache record for a block that just ran successfully. `before` is the fingerprint taken
    (from planned_manifest) right before the block was launched: the record is written only if the
    fingerprint of what the run's manifest says it read is the same now, i.e. no input or tool changed
    while it ran. False if not recorded, for that or because it could not be written."""
    manifest_path = block_out / ARTIFACTS["manifest"]
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    data = {"schema_version": CACHE_SCHEMA_VERSION, "fingerprint": fingerprint(argv, python, manifest),
            "artifacts": artifact_hashes(block_out)}
    if data["fingerprint"] is None or data["fingerprint"] != before or not complete(data):
        return False
    try:
        (block_out / RECORD).write_text(json.dumps(data, indent=1) + "\n")
    except OSError:
        return False
    return True


def complete(data: dict) -> bool:
    """A record can only ever match if every input (sources, includes, profile, Liberty), every yosys
    share/ file, the python, yosys and abc binaries, metrics.json and run_manifest.json hashed."""
    fp, artifacts = section(data, "fingerprint"), section(data, "artifacts")
    tools = section(fp, "tools")
    return (all(artifacts.get(ARTIFACTS[k]) is not None for k in ("metrics", "manifest"))
            and None not in section(fp, "inputs").values()
            and None not in section(fp, "yosys_share").values()
            and all(tools.get(t) is not None for t in ("python", "yosys", "abc")))


def forget(block_out: Path) -> bool:
    """Drop the record before a block reruns so an interrupted run never leaves a stale hit. False if it
    could not be removed (a directory of that name, no permission): the block reruns regardless, and a
    leftover record can only match again if the rerun reproduces every artifact byte-for-byte."""
    try:
        (block_out / RECORD).unlink(missing_ok=True)
    except OSError:
        return False
    return True


def is_hit(block_out: Path, argv: list[str], python: str) -> bool:
    """True only if the previous run's record matches every input, tool, module and artifact hash today."""
    try:
        data = json.loads((block_out / RECORD).read_text())
        manifest = json.loads((block_out / ARTIFACTS["manifest"]).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(manifest, dict):
        return False
    if not complete(data):
        return False
    fp = fingerprint(argv, python, manifest)
    if fp is None or data["fingerprint"] != fp:
        return False
    return data["artifacts"] == artifact_hashes(block_out)
