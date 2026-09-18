"""
suite_cache: skip a corpus block whose previous run is provably still valid.

After a block finishes with `status: ok`, run_suite.py drops `suite_cache.json` into the block's
output directory. It records the sha256 of everything the run depended on and of everything it
produced:

    argv            the exact bool_area.py command line (sources, top, defines, includes, extra args)
    inputs          every source file, every file they `include (transitively, resolved next to the
                    including file and then in the -I directories, as the frontends do), the profile
                    and its Liberty files
    include_dirs    every file under every -I directory
    tools           the python interpreter, yosys (slang is linked in), the abc binary yosys runs (its
                    sibling yosys-abc, or for a build with an external ABC that path unless $ABC is set,
                    as yosys's own `help abc` reports), sv2v, iverilog and vvp, each resolved the way
                    bool_area.py resolves it today (argv, $YOSYS/$SV2V/$ABC, ./build, PATH), so pointing
                    the environment at another binary is a miss
    yosys_share     every file in the share/ directory that yosys loads its `+/` support files from
                    (techmap.v, simcells.v, ...), since `synth` reads them at run time
    code            every tools/synth_area/*.py module
    artifacts       every path the flow owns (bool_area.owned_files): the fixed artifacts, sv2v output,
                    equiv_* scripts/logs and the whole sim/ directory; absent ones are recorded as absent

On the next run the block is reused only if every one of those hashes is unchanged and every
artifact is still on disk byte-for-byte (and nothing that was absent has appeared). Anything else (a
missing tool, an unreadable file, an older record) is a miss, never a guess. Failed runs are not
recorded: a block that failed reruns.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from bool_area import ARTIFACTS, FlowError, abc_executable, liberty_paths, load_profile, owned_files
from run_report import sha256_file
from synth_area import find_sv2v, find_yosys

CACHE_SCHEMA_VERSION = 2
RECORD = "suite_cache.json"
HERE = Path(__file__).resolve().parent
INCLUDE_RE = re.compile(r'^[ \t]*`include[ \t]+["<]([^">\n]+)[">]', re.MULTILINE)


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
    line counts, also ones in an inactive `ifdef branch (conservative). One that resolves nowhere is
    recorded under the including file as None, which keeps the block from being cached at all."""
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
        for name in INCLUDE_RE.findall(text):
            for d in (Path(src).parent, *map(Path, include_dirs)):
                if (d / name).is_file():
                    p = str(d / name)
                    found[p] = try_sha256(Path(p))
                    todo.append(p)
                    break
            else:
                found[f"{src}: `include {name}"] = None
    return found


def yosys_share_dir(yosys: str) -> Path | None:
    """Where this yosys reads its `+/` support files from, looked up as init_share_dirname in kernel/yosys.cc
    does from /proc/self/exe: `share/` beside the binary (a build tree), else `../share/yosys/` (an
    install), else the compiled-in data directory, which the `yosys-config` beside it reports as
    `--datdir`. None when none of those is a directory, which keeps the block from being cached."""
    exe = Path(yosys).resolve()
    candidates = [exe.parent / "share", exe.parent.parent / "share" / "yosys"]
    try:
        out = subprocess.run([str(exe.with_name("yosys-config")), "--datdir"], capture_output=True, text=True,
                             timeout=30, check=False)
        if out.returncode == 0 and out.stdout.strip():
            candidates.append(Path(out.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    return next((d for d in candidates if d.is_dir()), None)


@functools.cache
def share_hashes(yosys: str) -> dict[str, str | None]:
    """dir_hashes of yosys_share_dir(yosys) (a few hundred files, shared by every block: hash once)."""
    share = yosys_share_dir(yosys)
    return dir_hashes(share) if share else {"": None}


def code_hashes() -> dict[str, str | None]:
    return {p.name: try_sha256(p) for p in sorted(HERE.glob("*.py"))}


def option(argv: list[str], name: str) -> str | None:
    """Value of `--name X` / `--name=X` in a bool_area.py argument list (last one wins, as argparse)."""
    value = None
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            value = argv[i + 1]
        elif a.startswith(name + "="):
            value = a[len(name) + 1:]
    return value


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


def profile_files(profile_path: object) -> list[str]:
    """The profile and the Liberty files it names; [''] (hashes as None) if it cannot be loaded."""
    if not isinstance(profile_path, str):
        return [""]
    try:
        profile, _ = load_profile(profile_path)
        return [profile_path, *(str(p) for p in liberty_paths(profile, verify=False))]
    except (FlowError, OSError, ValueError):
        return [""]


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
        "inputs": {**{p: try_sha256(Path(p)) for p in [*sources, *profile_files(profile)]},
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


def record(block_out: Path, argv: list[str], python: str) -> bool:
    """Write the cache record for a block that just ran successfully. False if it could not be written."""
    manifest_path = block_out / ARTIFACTS["manifest"]
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    data = {"schema_version": CACHE_SCHEMA_VERSION, "fingerprint": fingerprint(argv, python, manifest),
            "artifacts": artifact_hashes(block_out)}
    if data["fingerprint"] is None or not complete(data):
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
