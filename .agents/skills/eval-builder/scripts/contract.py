"""The frozen task folder contract.

One contract, two readers. eval-builder writes the folder and freezes it, the evals command
(run, grade, pack, prove) reads it. The same file lives in skills/eval-builder/scripts and in
lib so each part installs on its own, the two copies are kept identical byte for byte.

A folder frozen on a laptop and one frozen in a Devin cloud session must be the same thing
to the evals command, so nothing in the frozen set may name a machine. Build machine details
(the clone path, the session store path) go in build-machine.json, which is neither hashed
nor shipped.
"""
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

# Frozen, hashed into approval.json, the evals command refuses the task if any changes.
REQUIRED = (
    "pr.json",            # the merged PR, metadata only, no clone path
    "suitability.md",     # first line Verdict yes, the three tests, the engineer's decision
    "prompt.md",          # the one shot prompt, with ## Context and ## Interface when needed
    "criteria.md",        # would merge checklist, blocking and advisory items
    "isolation.md",       # the rules prepended to every prompt
    "setup.json",         # repo_url, base_sha, merge_sha, setup_commands, env_names, services, setup_proof
    "setup-proof.txt",    # output of the setup commands on a fresh tree at base
    "hidden-test/test.sh",         # exit 0 present, 1 absent, 2 or more infrastructure
    "hidden-test/proof.json",      # ok, base exit 1, merge exit 0, wrong fix exit 1 when present
    "hidden-test/proof/base.txt",
    "hidden-test/proof/merge.txt",
    "leak-check.json",    # the leak check of the final prompt and hidden test
    "gates.json",         # the engineer's yes at suitability, proof and freeze, or a one shot record
)
OPTIONAL = (
    "session-excerpt.md",            # the engineer's human turns, redacted. For a PR fallback build it holds the PR body, linked issue and commit messages
    "session-source.json",           # where the ask came from, store kind, session id, prompt_source, sha256 of the raw source. No path
    "hidden-test/wrong-fix.patch",
    "hidden-test/proof/wrong-fix.txt",
)
# Everything else under hidden-test/ is frozen too (helper scripts, fixtures).
# Exactly one of session-excerpt.md or session-source.json with prompt_source must exist.

# Written by the build, never frozen, never shipped. Machine detail only.
BUILD_ONLY = (
    "build-machine.json",   # repo_path of the clone the builder used, store_path of the picked session
    "candidates.json",      # the sessions the finder listed, with their store paths
)
# Laptop side, written by the evals command, shipped but not frozen.
#   rejected.json          a build step refused, stage and reasons. Ships as a record
#   runs/laptop-proof.json the hidden test controls rerun on the laptop
#   runs/<label>/rN/       one attempt

GATES = ("suitability", "proof", "freeze")
# What each yes covers. gate.py records the sha256 of these files beside the yes and gates_problems refuses a
# task where one of them changed afterwards, so a yes is never carried over an edit the engineer did not see.
GATE_COVERS = {
    "suitability": ("pr.json", "suitability.md"),
    "proof": ("setup.json", "setup-proof.txt", "hidden-test"),
    "freeze": ("prompt.md", "criteria.md", "isolation.md", "session-excerpt.md", "session-source.json"),
}

PR_FIELDS = ("task_id", "repo", "repo_url", "number", "url", "title", "merged_at", "merge_sha", "head_sha", "head_ref", "base_ref")
PR_NEVER = ("repo_path",)            # a build machine path has no place in the frozen set
SETUP_FIELDS = ("repo_url", "base_sha", "merge_sha", "head_sha", "setup_commands", "env_names", "services", "setup_proof")
SOURCE_FIELDS = ("source", "prompt_source")
SOURCE_NEVER = ("store_path",)
SHA = re.compile(r"^[0-9a-f]{40}$")

# repo_url is what the laptop fetches the base commit from. It must be reachable from any
# machine of the customer's, so no proxy of the build machine, no local path, no credentials, no
# address literal and no name that stands for one machine. A private hostname the customer's
# laptops resolve over their network (a GitHub Enterprise server) is fine.
PRIVATE_HOSTS = ("localhost", "localhost.localdomain", "0.0.0.0", "::1")
ONE_MACHINE_SUFFIXES = (".localhost", ".local", ".localdomain", ".home.arpa",
                        ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me", ".vcap.me", ".traefik.me")
PROXY_MARKERS = ("git-manager", "/proxy/")
# EVAL_ALLOW_FILE_REPO_URL=1 lets a file:// repo_url through for a repository that has no remote. Such a
# task can only be rerun on the machine that built it and pack says so. Nothing sets it by default.
ALLOW_FILE_URL = "EVAL_ALLOW_FILE_REPO_URL"

# A frozen file that names a folder on the build machine is a leak of the engineer's disk layout and
# a path no other machine has. Placeholders (<repo>, <task>, $EVAL_HOME) are how the builder says it.
MACHINE_PATH = re.compile(r"(?<![\w.-])(?:/Users/[^/\s\"'<>]+/|/home/[^/\s\"'<>]+/|/root/|/private/var/folders/|/var/folders/|"
                          r"/opt/\.devin|/mnt/c/Users/|[A-Za-z]:\\Users\\)")
TEXT_SUFFIXES = ("", ".md", ".json", ".txt", ".sh", ".bash", ".py", ".toml", ".yaml", ".yml", ".patch", ".diff", ".cfg", ".ini", ".js", ".ts", ".csv")


def host_problem(host):
    """Why host is not a name every laptop of the customer's can resolve to the same git server, or None."""
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h:
        return "has no host"
    try:
        ipaddress.ip_address(h)
        return f"host {h} is an address literal, use the hostname of the git server"
    except ValueError:
        pass
    if h in PRIVATE_HOSTS or h.endswith(ONE_MACHINE_SUFFIXES):
        return f"host {h} stands for one machine, use the hostname of the git server"
    if "." not in h:
        return f"host {h} is not a hostname other machines resolve"
    return None


def repo_url_problem(url):
    """Why url cannot serve as the portable repo_url, or None."""
    if not url or not isinstance(url, str):
        return "repo_url is missing"
    u = urlsplit(url)
    if u.scheme == "file":
        if os.environ.get(ALLOW_FILE_URL) == "1":
            return None
        return "repo_url is a file:// path on one machine, use the https url of the repository"
    if u.scheme != "https":
        return f"repo_url must be https, got {u.scheme or 'no scheme'}"
    if "@" in u.netloc:
        return "repo_url carries credentials"
    if any(m in url for m in PROXY_MARKERS):
        return "repo_url points at a git proxy of the build machine, a laptop cannot fetch from it"
    why = host_problem(u.hostname)
    if why:
        return f"repo_url {why}"
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        return "repo_url has no owner/repository path"
    return None


def build_machine(task):
    """build-machine.json, the build machine's own notes (clone path, session store path). Empty when absent."""
    p = Path(task) / "build-machine.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def note_build_machine(task, **fields):
    """Merge fields into build-machine.json. Never frozen, never shipped."""
    p = Path(task) / "build-machine.json"
    data = build_machine(task)
    data.update(fields)
    p.write_text(json.dumps(data, indent=2) + "\n")
    return data


def frozen_files(task):
    task = Path(task)
    out = []
    for rel in REQUIRED + OPTIONAL:
        if (task / rel).is_file():
            out.append(rel)
    ht = task / "hidden-test"
    if ht.is_dir():
        for p in sorted(ht.rglob("*")):
            if p.is_file():
                rel = p.relative_to(task).as_posix()
                if rel not in out:
                    out.append(rel)
    return sorted(out)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_all(task):
    task = Path(task)
    return {rel: sha256_file(task / rel) for rel in frozen_files(task)}


def load_json(path):
    try:
        return json.loads(Path(path).read_text()), None
    except json.JSONDecodeError as e:
        return None, f"{Path(path).name} is not valid JSON, {e}"


def gate_cover_hashes(task, gate):
    """sha256 of every file the gate's yes covers, those that exist. A folder is walked, proof outputs included."""
    task = Path(task)
    out = {}
    for rel in GATE_COVERS[gate]:
        p = task / rel
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    out[f.relative_to(task).as_posix()] = sha256_file(f)
        elif p.is_file():
            out[rel] = sha256_file(p)
    return out


def gates_problems(task):
    """gates.json must hold the engineer's yes at each gate, tied to the files it covered, or one one_shot record with a reason."""
    p = Path(task) / "gates.json"
    if not p.is_file():
        return ["missing gates.json, record the engineer's yes with gate.py at suitability, proof and freeze"]
    data, err = load_json(p)
    if err:
        return [err]
    if (data.get("one_shot") or {}).get("reason"):
        return []
    gates = data.get("gates") or {}
    out = []
    for g in GATES:
        rec = gates.get(g) or {}
        if not rec.get("approved_by") or not rec.get("at") or not rec.get("said"):
            out.append(f"gates.json has no engineer yes for the {g} gate, run gate.py --gate {g}")
            continue
        covers = rec.get("covers")
        if not isinstance(covers, dict):
            out.append(f"gates.json {g} gate is not tied to the files it covered, record it again with gate.py --gate {g}, and if the task is already frozen delete approval.json on purpose and freeze again")
            continue
        now = gate_cover_hashes(task, g)
        changed = sorted(k for k in set(covers) | set(now) if covers.get(k) != now.get(k))
        if changed:
            out.append(f"{', '.join(changed)} changed after the {g} gate yes, ask the engineer again and run gate.py --gate {g}")
    return out


def machine_path_problems(task, allowed=()):
    """Frozen text files that name a folder on some machine, one line each. allowed holds strings to ignore
    (the test suite's file:// repo url)."""
    task = Path(task)
    out = []
    for rel in frozen_files(task):
        p = task / rel
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for s in allowed:
                if s:
                    line = line.replace(s, "")
            m = MACHINE_PATH.search(line)
            if m:
                out.append(f"{rel}:{n} names a machine path, {m.group(0)[:50]}, use a placeholder such as <repo> or $EVAL_HOME")
                break
    return out


# Every key approval.json may carry. build_path (local or devin-cloud) is the only note about the build machine,
# the OS and Python of the setup proof live in setup.json where evals doctor <task> reads them.
APPROVAL_KEYS = frozenset(("engineer", "approved_at", "build_path", "lint_acknowledged", "lint_acknowledged_sha256",
                           "approves", "runs_and_grades", "gates", "one_shot", "sha256"))


def check_folder(task, need_approval=True):
    """Problems with a task folder, empty list when it meets the contract."""
    task = Path(task)
    problems = []
    for rel in REQUIRED:
        if not (task / rel).is_file():
            problems.append(f"missing {rel}")
    if not (task / "session-excerpt.md").is_file() and not (task / "session-source.json").is_file():
        problems.append("missing session-excerpt.md, or session-source.json with prompt_source for a PR fallback build")
    datas = {}
    for name, fields in (("pr.json", PR_FIELDS), ("setup.json", SETUP_FIELDS)):
        if not (task / name).is_file():
            continue
        data, err = load_json(task / name)
        if err:
            problems.append(err)
            continue
        datas[name] = data
        for f in fields:
            if f not in data or data[f] in (None, ""):
                problems.append(f"{name} lacks {f}")
        if name == "pr.json":
            for f in PR_NEVER:
                if f in data:
                    problems.append(f"pr.json carries {f}, a build machine path, it belongs in build-machine.json")
        if name == "setup.json":
            for f in ("base_sha", "merge_sha"):
                if data.get(f) and not SHA.match(str(data[f])):
                    problems.append(f"setup.json {f} is not a full 40 char sha")
            if not (data.get("setup_proof") or {}).get("ok"):
                problems.append("setup.json setup_proof is not ok")
        why = repo_url_problem(data.get("repo_url"))
        if why:
            problems.append(f"{name} {why}")
    if "pr.json" in datas and "setup.json" in datas and datas["pr.json"].get("repo_url") != datas["setup.json"].get("repo_url"):
        problems.append("pr.json and setup.json disagree on repo_url")
    if (task / "session-source.json").is_file():
        data, err = load_json(task / "session-source.json")
        if err:
            problems.append(err)
        else:
            for f in SOURCE_FIELDS:
                if not data.get(f):
                    problems.append(f"session-source.json lacks {f}")
            for f in SOURCE_NEVER:
                if f in data:
                    problems.append(f"session-source.json carries {f}, a build machine path, it belongs in build-machine.json")
    if (task / "suitability.md").is_file() and not re.search(r"^verdict\s+yes\b", (task / "suitability.md").read_text(), re.M | re.I):
        problems.append("suitability.md has no line starting with 'Verdict yes'")
    if (task / "hidden-test" / "proof.json").is_file():
        data, err = load_json(task / "hidden-test" / "proof.json")
        if err:
            problems.append(err)
        elif not data.get("ok"):
            problems.append("hidden-test/proof.json is not ok")
    if (task / "gates.json").is_file():
        problems += gates_problems(task)
    problems += machine_path_problems(task, [d.get("repo_url") for d in datas.values() if not repo_url_problem(d.get("repo_url"))])
    if need_approval:
        ap = task / "approval.json"
        if not ap.is_file():
            problems.append("missing approval.json")
        else:
            saved, err = load_json(ap)
            if err:
                saved = {}
                problems.append(err)
            if not saved.get("engineer"):
                problems.append("approval.json has no engineer")
            extra = sorted(set(saved) - APPROVAL_KEYS)
            if extra:
                problems.append("approval.json carries " + ", ".join(extra) + ", the contract allows only " + ", ".join(sorted(APPROVAL_KEYS))
                                + ", build_path is the only note about the build machine, freeze again")
            missing = sorted(set(frozen_files(task)) - set(saved.get("sha256") or {}))
            if missing:
                problems.append("approval.json does not hash " + ", ".join(missing))
            if saved and ("lint_acknowledged" not in saved or "lint_acknowledged_sha256" not in saved):
                problems.append("approval.json carries no lint acknowledgement record, it was written by an older freeze or edited since, freeze again")
            elif saved and saved["lint_acknowledged_sha256"] != hashlib.sha256(
                    json.dumps(saved["lint_acknowledged"] or [], sort_keys=True, separators=(",", ":")).encode()).hexdigest():
                problems.append("the lint acknowledgements in approval.json were edited after the freeze")
    return problems
