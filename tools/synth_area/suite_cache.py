"""
suite_cache: skip a corpus block whose previous run is provably still valid.

After a block finishes with `status: ok`, run_suite.py drops `suite_cache.json` into the block's
output directory. It records the sha256 of everything the run depended on and of everything it
produced:

    argv            the exact bool_area.py command line (sources, top, defines, includes, extra args)
    inputs          every source file, include-directory file, the profile and its Liberty files
    tools           yosys (slang is linked in), yosys-abc, sv2v, iverilog, vvp
    code            every tools/synth_area/*.py module
    artifacts       every file bool_area.py wrote, including metrics.json and run_manifest.json

On the next run the block is reused only if every one of those hashes is unchanged and every
artifact is still on disk byte-for-byte. Anything else (a missing tool, an unreadable file, an
older record) is a miss, never a guess. Failed runs are not recorded: a block that failed reruns.
"""

from __future__ import annotations

import functools
import json
import shutil
from pathlib import Path

from bool_area import ARTIFACTS, FlowError, liberty_paths, load_profile
from run_report import sha256_file
from synth_area import find_sv2v

CACHE_SCHEMA_VERSION = 1
RECORD = "suite_cache.json"
HERE = Path(__file__).resolve().parent


def try_sha256(path: Path) -> str | None:
    try:
        return sha256_file(path) if path.is_file() else None
    except OSError:
        return None


@functools.cache
def tool_sha256(path: str) -> str | None:
    """Tool binaries are shared by every block of a run and large (yosys is ~40 MB): hash once."""
    return try_sha256(Path(path))


def dir_hashes(root: Path) -> dict[str, str | None]:
    """{relative path: sha256} for every regular file under `root` (an include directory)."""
    if not root.is_dir():
        return {"": None}
    return {str(p.relative_to(root)): try_sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def code_hashes() -> dict[str, str | None]:
    return {p.name: try_sha256(p) for p in sorted(HERE.glob("*.py"))}


def tool_paths(yosys: str | None) -> dict[str, str | None]:
    yosys_abc = str(Path(yosys).with_name("yosys-abc")) if yosys else None
    return {"yosys": yosys, "yosys-abc": yosys_abc, "sv2v": find_sv2v(None),
            "iverilog": shutil.which("iverilog"), "vvp": shutil.which("vvp")}


def profile_files(profile_path: object) -> list[str]:
    """The profile and the Liberty files it names; [''] (hashes as None) if it cannot be loaded."""
    if not isinstance(profile_path, str):
        return [""]
    try:
        profile, _ = load_profile(profile_path)
    except FlowError:
        return [""]
    return [profile_path, *(str(p) for p in liberty_paths(profile, verify=False))]


def section(container: dict, key: str) -> dict:
    v = container.get(key)
    return v if isinstance(v, dict) else {}


def fingerprint(argv: list[str], python: str, manifest: dict) -> dict:
    """Hashes of everything a bool_area.py run with `argv` depends on. The manifest of the run being
    recorded (or reused) says which files those were: sources, include dirs, profile, yosys."""
    yosys = section(manifest, "generated_by").get("yosys")
    yosys = yosys if isinstance(yosys, str) else None
    profile = section(manifest, "profile").get("path")
    sources = [str(s.get("path")) for s in manifest.get("sources", []) if isinstance(s, dict)] or [""]
    includes = section(manifest, "frontend").get("include_dirs")
    includes = [str(d) for d in includes] if isinstance(includes, list) else []
    return {
        "argv": list(argv),
        "python": python,
        "inputs": {p: try_sha256(Path(p)) for p in [*sources, *profile_files(profile)]},
        "include_dirs": {d: dir_hashes(Path(d)) for d in includes},
        "tools": {name: tool_sha256(path) if path else None for name, path in tool_paths(yosys).items()},
        "code": code_hashes(),
    }


def artifact_hashes(block_out: Path, manifest: dict) -> dict[str, str | None]:
    """sha256 of every file the run produced, keyed by artifact name; None for anything not on disk."""
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    hashes: dict[str, str | None] = {}
    for key, entry in artifacts.items():
        if isinstance(entry, dict) and entry.get("exists"):
            hashes[key] = try_sha256(Path(str(entry.get("path"))))
    for key in ("metrics", "manifest"):
        hashes[key] = try_sha256(block_out / ARTIFACTS[key])
    return hashes


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
            "artifacts": artifact_hashes(block_out, manifest)}
    if not complete(data):
        return False
    try:
        (block_out / RECORD).write_text(json.dumps(data, indent=1) + "\n")
    except OSError:
        return False
    return True


def complete(data: dict) -> bool:
    """A record can only ever match if every input, the yosys binary and every artifact hashed."""
    fp, artifacts = section(data, "fingerprint"), section(data, "artifacts")
    return (bool(artifacts) and None not in artifacts.values()
            and None not in section(fp, "inputs").values()
            and section(fp, "tools").get("yosys") is not None)


def forget(block_out: Path) -> None:
    """Drop the record before a block reruns so an interrupted run never leaves a stale hit."""
    (block_out / RECORD).unlink(missing_ok=True)


def is_hit(block_out: Path, argv: list[str], python: str) -> bool:
    """True only if the previous run's record matches every input, tool, module and artifact hash today."""
    try:
        data = json.loads((block_out / RECORD).read_text())
        manifest = json.loads((block_out / ARTIFACTS["manifest"]).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(manifest, dict):
        return False
    if not complete(data) or data["fingerprint"] != fingerprint(argv, python, manifest):
        return False
    return data["artifacts"] == artifact_hashes(block_out, manifest)
