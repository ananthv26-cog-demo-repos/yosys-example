#!/usr/bin/env python3
"""Pin the base commit and write setup.json.

Usage
  base_commit.py --task-dir DIR [--base SHA --reason TEXT]

Computes two candidates and refuses to guess when they disagree or when neither
can be computed (rejected.json records why).
  a. merge-base(PR head, first parent of the merge commit)   what the branch grew from
  b. parent of the first PR commit                            what the engineer saw on day one
Fetches refs/pull/N/head so this works after the branch was deleted.
Writes setup.json with repo_url, base_sha, merge_sha, head_sha and empty
setup_commands, env_names, services for the skill to fill in.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import build_machine  # noqa: E402


def git(args, cwd, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and p.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed\n{p.stderr.strip()}")
    return p.stdout.strip()


def has_commit(repo, sha):
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True).returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--base", help="override, full 40 char sha, only after the two candidates disagreed")
    ap.add_argument("--reason", help="why the override is right, recorded in setup.json")
    ap.add_argument("--repo-path", help="clone to read commits from, default the one noted in build-machine.json")
    a = ap.parse_args()
    task = Path(a.task_dir)
    pr = json.loads((task / "pr.json").read_text())
    repo = a.repo_path or build_machine(task).get("repo_path") or pr.get("repo_path")
    if not repo or not Path(repo).expanduser().is_dir():
        sys.exit("no clone to read from. build-machine.json names none (run pr_info.py from inside a clone) and --repo-path was not given.")
    repo = str(Path(repo).expanduser())
    n = pr["number"]

    if not has_commit(repo, pr["merge_sha"]):
        git(["fetch", "--quiet", "origin", pr["merge_sha"]], repo, check=False)
    if not has_commit(repo, pr["merge_sha"]):
        sys.exit(f"merge commit {pr['merge_sha'][:12]} is not in {repo} and could not be fetched from origin. Run git fetch origin there and retry.")
    git(["fetch", "--quiet", "origin", f"refs/pull/{n}/head:refs/eval/pr-{n}-head"], repo, check=False)
    head = git(["rev-parse", "--verify", "--quiet", f"refs/eval/pr-{n}-head"], repo, check=False) or pr.get("head_sha")
    if head and not has_commit(repo, head):
        head = None

    parents = git(["rev-list", "--parents", "-n", "1", pr["merge_sha"]], repo).split()[1:]
    merge_style = "merge commit" if len(parents) == 2 else "squash or rebase"
    main_before = parents[0]

    cand_a = git(["merge-base", head, main_before], repo, check=False) if head else None
    first = pr.get("first_commit_sha")
    cand_b = None
    if first and has_commit(repo, first):
        cand_b = git(["rev-parse", f"{first}^"], repo, check=False) or None

    print(f"merge {pr['merge_sha'][:12]} ({merge_style}), main before merge {main_before[:12]}, PR head {(head or '?')[:12]}")
    print(f"  a. merge-base(head, main before merge)  {cand_a or 'unavailable'}")
    print(f"  b. parent of first PR commit            {cand_b or 'unavailable'}")

    if a.base:
        if len(a.base) != 40 or not a.reason:
            sys.exit("--base needs a full 40 char sha and --reason")
        base, how = a.base, f"engineer override, {a.reason}"
    elif cand_a and cand_b and cand_a != cand_b:
        print("\nThe two candidates disagree, the branch was rebased or had the base branch merged in.")
        print("Look at both with  git log --oneline -3 <sha>  and rerun with --base <sha> --reason '...'.")
        print("Usual pick is a, the tree the branch actually grew from, unless the diff a..merge pulls in unrelated work.")
        sys.exit(2)
    elif cand_a or cand_b:
        base = cand_a or cand_b
        how = "both candidates agree" if cand_a and cand_b else ("merge-base only" if cand_a else "parent of first commit only")
    else:
        rec = {"rejected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "stage": "base_commit",
               "reasons": ["neither base candidate could be computed, the PR head and first commit are not reachable from this clone"]}
        (task / "rejected.json").write_text(json.dumps(rec, indent=2))
        print("\nNeither candidate could be computed. Fetch the PR head (git fetch origin refs/pull/N/head) and retry,")
        print("or name the base yourself with --base <full sha> --reason '...'. Guessing the merge parent is refused, rejected.json written.")
        sys.exit(2)

    if len(base) != 40:
        sys.exit("base sha is not 40 chars, refusing")
    stat = git(["diff", "--shortstat", base, pr["merge_sha"]], repo)
    print(f"\nbase {base}\n  {how}\n  solution diff base..merge, {stat}")
    if len(parents) == 2 and cand_a and git(["rev-list", "--count", f"{cand_a}..{main_before}"], repo) != "0":
        print("  note, the base branch moved between the branch point and the merge, the hidden test is proven on the merge commit so this is fine")

    (task / "rejected.json").unlink(missing_ok=True)
    setup_path = task / "setup.json"
    setup = json.loads(setup_path.read_text()) if setup_path.exists() else {}
    setup.update({
        "repo": pr["repo"], "repo_url": pr["repo_url"], "base_sha": base, "base_how": how,
        "merge_sha": pr["merge_sha"], "head_sha": head, "merge_style": merge_style,
        "solution_files": pr.get("files"),
    })
    setup.setdefault("setup_commands", [])
    setup.setdefault("env_names", [])
    setup.setdefault("services", [])
    setup.setdefault("runtime_notes", "")
    setup_path.write_text(json.dumps(setup, indent=2))
    print(f"wrote {setup_path}. Fill setup_commands, env_names (names only), services, then run prove.py setup.")


if __name__ == "__main__":
    main()
