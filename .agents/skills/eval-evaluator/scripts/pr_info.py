#!/usr/bin/env python3
"""Write <evals>/<task-id>/pr.json for a merged PR and print a suitability worksheet.

Usage
  pr_info.py <pr-number> [--repo-path PATH] [--evals DIR]

Needs `gh` logged in and a clone of the repo (cwd or --repo-path).
Everything here is plain git and gh, nothing agent specific.
"""
import argparse
import datetime as dt
import json
import os
import re
import platform
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

GH_FIELDS = (
    "number,url,title,body,author,headRefName,headRefOid,baseRefName,mergedAt,"
    "mergeCommit,commits,files,closingIssuesReferences,additions,deletions,"
    "changedFiles,reviews,comments"
)

LOCKFILES = re.compile(
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|Cargo\.lock|poetry\.lock|"
    r"uv\.lock|go\.sum|Gemfile\.lock|composer\.lock|Pipfile\.lock|bun\.lockb?)$"
)
DOCS = re.compile(r"(\.(md|mdx|rst|txt|adoc)$)|(^|/)docs?/", re.I)
GENERATED = re.compile(r"__generated__|\.generated\.|__snapshots__|\.snap$|/generated/", re.I)
MECHANICAL_TITLE = re.compile(
    r"^\s*(\[?\w*\]?\s*)?(bump|revert|rename|merge|chore\(deps\)|deps:|update dependenc|upgrade)",
    re.I,
)
BOT_AUTHORS = {"dependabot", "dependabot[bot]", "renovate", "renovate[bot]", "github-actions[bot]"}
EXTERNAL_LINKS = re.compile(
    r"https?://[^\s)>\]]*(sentry\.io|linear\.app|atlassian\.net|datadoghq\.com|slack\.com|"
    r"notion\.so|grafana|pagerduty|zendesk|intercom|figma\.com|docs\.google\.com|"
    r"looker|metabase|honeycomb\.io|newrelic)[^\s)>\]]*",
    re.I,
)
PAD_HOURS = 24


def sh(args, cwd=None):
    p = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}): {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def iso_to_unix(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def portable_repo_url(remote_url, name_with_owner, gh_url=None):
    """The https url any machine of the customer's can fetch the repo from.

    origin is often not that. A laptop may use ssh, and a Devin machine sees the repo through a
    git proxy of its own (https://<id>.git-manager.../proxy/github.com/owner/repo), which no
    laptop can reach. Prefer what gh reports as the repository url, else rebuild it from the
    host in the remote and the owner/name gh gave us.
    """
    if gh_url and gh_url.startswith("https://") and "@" not in urlsplit(gh_url).netloc:
        return gh_url.rstrip("/").removesuffix(".git")
    remote = (remote_url or "").strip()
    host = None
    m = re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)(?::\d+)?[:/]", remote)
    if remote.startswith(("https://", "http://", "ssh://")) or "@" in remote.split("/")[0]:
        if remote.startswith(("https://", "http://", "ssh://")):
            u = urlsplit(remote)
            host = u.hostname
            proxied = re.search(r"/proxy/([\w.-]+)/", u.path)
            if proxied:
                host = proxied.group(1)
        elif m:
            host = m.group(1)
    if not host or "git-manager" in host or host in ("localhost", "127.0.0.1"):
        host = "github.com"
    return f"https://{host}/{name_with_owner}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo-path", default=".")
    ap.add_argument("--evals", default=os.environ.get("EVAL_HOME", str(Path.home() / "evals")))
    a = ap.parse_args()

    repo_path = Path(a.repo_path).resolve()
    top = sh(["git", "rev-parse", "--show-toplevel"], cwd=repo_path).strip()
    repo_path = Path(top).resolve()
    name_with_owner = sh(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=repo_path).strip()
    remote_url = sh(["git", "remote", "get-url", "origin"], cwd=repo_path).strip()
    gh_url = subprocess.run(["gh", "repo", "view", "--json", "url", "-q", ".url"], cwd=repo_path, text=True, capture_output=True).stdout.strip()
    repo_url = portable_repo_url(remote_url, name_with_owner, gh_url)

    pr = json.loads(sh(["gh", "pr", "view", str(a.pr), "--json", GH_FIELDS], cwd=repo_path))
    if not pr.get("mergedAt") or not pr.get("mergeCommit"):
        sys.exit(f"PR {a.pr} is not merged. Only merged PRs become evals.")

    commits = sorted(pr.get("commits") or [], key=lambda c: c.get("authoredDate") or c.get("committedDate") or "")
    first_commit = commits[0] if commits else None
    merged_unix = iso_to_unix(pr["mergedAt"])
    first_unix = iso_to_unix(first_commit["authoredDate"]) if first_commit else merged_unix - 7 * 86400
    files = [f["path"] for f in pr.get("files") or []]

    task_id = f"{name_with_owner.split('/')[-1]}-pr-{pr['number']}".lower()
    out = {
        "task_id": task_id,
        "repo": name_with_owner,
        "repo_url": repo_url,
        "number": pr["number"],
        "url": pr["url"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "author": (pr.get("author") or {}).get("login"),
        "head_ref": pr["headRefName"],
        "head_sha": pr.get("headRefOid"),
        "base_ref": pr["baseRefName"],
        "merged_at": pr["mergedAt"],
        "merge_sha": pr["mergeCommit"]["oid"],
        "first_commit_sha": first_commit["oid"] if first_commit else None,
        "commit_shas": [c["oid"] for c in commits],
        "commit_messages": [c.get("messageHeadline", "") for c in commits],
        "files": files,
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "linked_issues": [
            {"number": i.get("number"), "title": i.get("title"), "url": i.get("url"), "body": i.get("body", "")}
            for i in pr.get("closingIssuesReferences") or []
        ],
        "review_comments": [
            {"author": (r.get("author") or {}).get("login"), "body": r.get("body", "")}
            for r in (pr.get("reviews") or []) + (pr.get("comments") or [])
            if r.get("body")
        ],
        "window_start_unix": first_unix - PAD_HOURS * 3600,
        "window_end_unix": merged_unix,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }

    task_dir = Path(a.evals).expanduser() / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "pr.json").write_text(json.dumps(out, indent=2))
    # The clone path is this machine's business only. It stays out of pr.json, which is frozen and shipped.
    bm_path = task_dir / "build-machine.json"
    bm = json.loads(bm_path.read_text()) if bm_path.is_file() else {}
    bm.update({"repo_path": str(repo_path), "remote_url": remote_url, "host": platform.node(), "os": platform.platform(),
               "note": "build machine detail, never frozen, never shipped"})
    bm_path.write_text(json.dumps(bm, indent=2) + "\n")
    if remote_url.rstrip("/").removesuffix(".git") != repo_url:
        print(f"repo_url recorded as {repo_url} (origin here is {remote_url}, which other machines may not reach)")

    # Suitability worksheet. Tests 1 and 2 run from the PR alone, test 3 needs the session.
    flags, warnings = [], []
    if not files:
        flags.append("no files changed")
    if files and all(LOCKFILES.search(f) for f in files):
        flags.append("lockfile only change")
    if files and all(DOCS.search(f) for f in files):
        flags.append("docs only change")
    if files and all(GENERATED.search(f) for f in files):
        flags.append("generated files only")
    if MECHANICAL_TITLE.search(pr["title"] or ""):
        flags.append(f"title looks mechanical ({pr['title'][:60]})")
    if (out["author"] or "").lower() in BOT_AUTHORS:
        flags.append(f"authored by a bot ({out['author']})")
    if len(files) > 30:
        warnings.append(f"{len(files)} files touched, a one shot prompt may not describe this well")
    dirs = {f.split("/")[0] for f in files}
    if len(dirs) > 4:
        warnings.append(f"touches {len(dirs)} top level areas ({', '.join(sorted(dirs)[:6])})")
    age_days = (dt.datetime.now(dt.timezone.utc).timestamp() - merged_unix) / 86400
    if age_days > 30:
        warnings.append(f"merged {age_days:.0f} days ago, Claude Code transcripts default to 30 day retention")
    links = sorted(set(m.group(0) for m in EXTERNAL_LINKS.finditer(out["body"] + " " + " ".join(i.get("body") or "" for i in out["linked_issues"]))))

    print(f"wrote {task_dir / 'pr.json'}")
    print(f"\nPR {pr['number']}  {pr['title']}")
    print(f"  {len(files)} files, +{pr.get('additions')} -{pr.get('deletions')}, merged {pr['mergedAt']}, head {pr['headRefName']}")
    print(f"  window {dt.datetime.fromtimestamp(out['window_start_unix']).isoformat(timespec='minutes')} to {dt.datetime.fromtimestamp(out['window_end_unix']).isoformat(timespec='minutes')} local time")
    print("\nSuitability worksheet")
    print("  Test 1, checkable behaviour change. Write the sentence 'call X with Y, before the PR you get A, after you get B'.")
    print("  Test 2, real work not mechanical.")
    for f in flags:
        print(f"    NO   {f}")
    for w in warnings:
        print(f"    WARN {w}")
    if not flags and not warnings:
        print("    no automatic flags")
    print("  Test 3, self contained. Finalise after the session is picked. Outside sources seen in the PR text so far:")
    if links:
        for l in links:
            print(f"    {l}")
    else:
        print("    none in the PR body or linked issues")
    print("\nTouched files")
    for f in files[:40]:
        print(f"  {f}")
    if len(files) > 40:
        print(f"  ... {len(files) - 40} more")


if __name__ == "__main__":
    main()
