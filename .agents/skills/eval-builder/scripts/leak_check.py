#!/usr/bin/env python3
"""Warn when prompt.md gives away the PR or its solution, and check that the
hidden test only needs names the prompt states.

Usage
  leak_check.py --task-dir DIR

Hard flags (must fix). PR number, branch name, commit shas, PR or issue URLs,
names of files the PR added, the hidden test folder, and any name the hidden
test uses that the PR introduced but the prompt's `## Interface` does not list.
Soft warnings (engineer judges). Identifiers the diff introduced and 6 word runs
shared with the added lines, both outside `## Interface`, and `## Interface`
names the hidden test never uses.

`## Interface` is the only part of the prompt that may name what the PR
introduced, and only what the hidden test needs, a function, parameter, flag,
key or exact message. A name counts as introduced when an added line outside
tests and docs has it and the tree at base does not. Comments in the hidden
test are ignored. freeze.py runs this again on the final prompt and test.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import build_machine  # noqa: E402


def repo_with(pr, setup):
    """A git dir holding base and merge. The build clone when build-machine.json names one, else a fetch from repo_url."""
    shas = [setup["base_sha"], setup["merge_sha"]]

    def has(d):
        return all(subprocess.run(["git", "cat-file", "-e", f"{x}^{{commit}}"], cwd=d, capture_output=True).returncode == 0 for x in shas)

    if pr.get("repo_path") and Path(pr["repo_path"]).expanduser().is_dir() and has(Path(pr["repo_path"]).expanduser()):
        return str(Path(pr["repo_path"]).expanduser())
    d = tempfile.mkdtemp(prefix="eval-leak-")
    subprocess.run(["git", "init", "-q", "--bare"], cwd=d, check=True)
    subprocess.run(["git", "remote", "add", "origin", setup["repo_url"]], cwd=d, check=True)
    for args in (["fetch", "--quiet", "origin", *shas], ["fetch", "--quiet", "origin"]):
        subprocess.run(["git", *args], cwd=d, capture_output=True)
        if has(d):
            return d
    sys.exit(f"could not find {shas[0][:12]} and {shas[1][:12]} in the build clone or fetch them from {setup['repo_url']}")

WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
DASHED = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)+")
STANDALONE = re.compile(r"(?<![\w-])[A-Za-z_][A-Za-z0-9_]{2,}(?![\w-])")
IDENT_DEF = re.compile(r"\b(?:def|class|fn|func|function|const|let|var|type|interface|struct|enum|pub fn|export (?:default )?(?:function|class|const))\s+([A-Za-z_][A-Za-z0-9_]{3,})")
COMMON = set("self this that with from into return async await const value values result results error errors data list item items type types test tests file files name names true false null none".split())
INTERFACE = re.compile(r"^##[ \t]+Interface[ \t]*$(.*?)(?=^##[ \t]|\Z)", re.M | re.S | re.I)
NOT_SOURCE = re.compile(r"(^|/)(tests?|specs?|__tests__|testdata|fixtures|docs?)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$"
                        r"|\.(md|rst|txt|adoc)$|(^|/)(CHANGES|CHANGELOG|HISTORY|NEWS)[^/]*$", re.I)
HASH_COMMENTS = {"", ".sh", ".bash", ".py", ".rb", ".pl", ".r", ".yml", ".yaml", ".toml"}
SLASH_COMMENTS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".cs", ".swift", ".php"}


def words(s):
    return [w.lower() for w in re.findall(r"[a-z0-9]+", s.lower())]


def ngrams(ws, n=6):
    return {" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def names(text):
    """Identifiers and dashed names. A dashed name (X-Robots-Private) is one name, its pieces (Robots, Private) are names
    of their own only where the text also uses them on their own."""
    return set(STANDALONE.findall(text)) | set(DASHED.findall(text))

def split_interface(prompt):
    """The body of the ## Interface section, and the prompt without it."""
    m = INTERFACE.search(prompt)
    return (m.group(1), prompt[:m.start()] + prompt[m.end():]) if m else ("", prompt)


def added_by_file(diff):
    out, cur, header = {}, None, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur, header = None, True
        elif header and line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            header = False
        elif cur and not header and line.startswith("+"):
            out.setdefault(cur, []).append(line[1:])
    return out


def hidden_test_code(task):
    """Every hidden test file without its comments, the proofs and the wrong fix left out."""
    root = task / "hidden-test"
    out = {}
    for p in sorted(root.rglob("*")) if root.is_dir() else []:
        rel = p.relative_to(root)
        if (not p.is_file() or rel.parts[0] == "proof" or "__pycache__" in rel.parts
                or rel.name in ("proof.json", "wrong-fix.patch") or p.stat().st_size > 1_000_000):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix in SLASH_COMMENTS:
            text = re.sub(r"(^|\s)//.*", r"\1", re.sub(r"/\*.*?\*/", " ", text, flags=re.S))
        elif p.suffix in HASH_COMMENTS:
            text = re.sub(r"(^|\s)#.*", r"\1", text)
        out[rel.as_posix()] = text
    return out


def introduced(repo, base_sha, by_file, candidates):
    """The candidates that an added source line has and the tree at base does not."""
    src = set()
    for f, lines in by_file.items():
        if not NOT_SOURCE.search(f):
            src |= names("\n".join(lines))
    new = set()
    for n in sorted(candidates & src):
        if n.lower() in COMMON:
            continue
        if subprocess.run(["git", "grep", "-q", "-w", "-F", "-e", n, base_sha], cwd=repo, capture_output=True).returncode == 1:
            new.add(n)
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    a = ap.parse_args()
    task = Path(a.task_dir)
    pr = json.loads((task / "pr.json").read_text())
    pr["repo_path"] = build_machine(task).get("repo_path") or pr.get("repo_path")
    # setup.json is written by base_commit.py (step 2). Before that only the prompt text can be checked, the
    # diff based checks need the base and merge commits it names.
    setup_path = task / "setup.json"
    setup = json.loads(setup_path.read_text()) if setup_path.is_file() else None
    prompt = (task / "prompt.md").read_text()
    low = prompt.lower()
    interface, rest = split_interface(prompt)
    by_file, added, created = {}, [], []
    if setup:
        repo = repo_with(pr, setup)
        diff = subprocess.run(["git", "diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", setup["base_sha"], setup["merge_sha"]],
                              cwd=repo, text=True, capture_output=True).stdout
        by_file = added_by_file(diff)
        added = [line for lines in by_file.values() for line in lines]
        new_files = re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)
        deleted = set(re.findall(r"^--- a/(.+)$", diff, re.M))
        created = [f for f in new_files if f not in deleted]

    hard, soft = [], []
    if re.search(rf"(?<![\d])#?{pr['number']}(?![\d])", prompt) and re.search(rf"(pr|pull|#)\s*{pr['number']}\b", low):
        hard.append(f"mentions the PR number {pr['number']}")
    ref = pr["head_ref"].lower()
    if ref in low:
        pr_text = ((pr.get("title") or "") + " " + (pr.get("body") or "")).lower()
        plain_word = re.fullmatch(r"[a-z]+", ref) and re.search(rf"\b{ref}\b", pr_text)
        if plain_word:
            soft.append(f"mentions the branch name {pr['head_ref']}, a plain word that the PR title or body also uses, keep it only if the interface needs that word")
        else:
            hard.append(f"mentions the branch name {pr['head_ref']}")
    shas = [setup["base_sha"], setup["merge_sha"], setup.get("head_sha") or ""] if setup else [pr.get("merge_sha") or "", pr.get("head_sha") or ""]
    for sha in shas + (pr.get("commit_shas") or []):
        if sha and sha[:7].lower() in low:
            hard.append(f"mentions commit {sha[:12]}")
    if re.search(r"github\.com/[^\s]+/(pull|issues)/\d+", low):
        hard.append("links a pull request or issue on GitHub")
    for f in created:
        base = f.rsplit("/", 1)[-1].lower()
        if len(base) > 6 and base in rest.lower():
            hard.append(f"names a file the PR created, {f}")
        elif len(base) > 6 and base in interface.lower():
            soft.append(f"## Interface names {f}, a file the PR created, keep it only if the hidden test needs that file")
    if "hidden-test" in low or "test.sh" in low:
        hard.append("mentions the hidden test")
    for kw in ("root cause", "the fix is", "the bug is in", "change line", "replace the call"):
        if kw in low:
            soft.append(f"phrase '{kw}' tends to state the solution, describe the symptom instead")

    idents = set()
    for line in added:
        for m in IDENT_DEF.finditer(line):
            idents.add(m.group(1))
    leaked = sorted(w for w in idents & set(WORD.findall(rest)) if w.lower() not in COMMON)
    if leaked:
        soft.append("identifiers introduced by the diff appear in the prompt outside ## Interface, " + ", ".join(leaked[:12]))

    shared = ngrams(words(rest)) & ngrams(words("\n".join(added)))
    if shared:
        soft.append(f"{len(shared)} six word runs shared with the added lines, e.g. '{sorted(shared)[0]}'")

    code = hidden_test_code(task)
    check = None
    if code and setup:
        used = set().union(*map(names, code.values()))
        ticked = set().union(*map(names, re.findall(r"`([^`\n]+)`", interface)))
        new = introduced(repo, setup["base_sha"], by_file, used | ticked)
        missing = sorted((new & used) - names(interface))
        if missing:
            hard.append("the hidden test uses " + ", ".join(missing) + ", which the PR introduced and the prompt does not state, "
                        "list it under ## Interface in prompt.md or change the test so it does not need it")
        unused = sorted((new & ticked) - used)
        if unused:
            soft.append("## Interface names " + ", ".join(unused) + ", which the PR introduced and the hidden test does not use, "
                        "keep that section to what the test needs")
        check = {"hidden_test_needs": sorted(new & used), "missing_from_interface": missing}

    for h in hard:
        print(f"HARD  {h}")
    for s in soft:
        print(f"SOFT  {s}")
    if not hard and not soft:
        print("no leaks flagged")
    if setup is None:
        print("setup.json is not written yet, so only the prompt text was checked, the diff based checks run once base_commit.py has run (step 2), freeze.py also runs them")
    elif check is None:
        print("no hidden test yet, so the names it needs were not checked, run this again after step 3, freeze.py also runs it")
    (task / "leak-check.json").write_text(json.dumps({"hard": hard, "soft": soft, "interface": check, "diff_checked": setup is not None}, indent=2))
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
