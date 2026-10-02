#!/usr/bin/env python3
"""Find the engineer's own agent session behind a merged PR.

Usage
  find_session.py --task-dir <evals>/<task-id> [--widen-days N] [--limit 5]

Reads pr.json, searches only the known session stores for this OS
(Devin CLI, Claude Code, Codex CLI, OpenCode), keeps sessions that ran inside
the repo during the PR window, ranks them, prints the top few and writes
candidates.json (this machine only, it holds store paths and is never shipped).
It never auto selects, and it says so when two candidates are close. Sessions
that started after the merge, or that read like the evaluation itself, are set
aside and listed, the session running this script is the usual one. Run
pick_session.py after the engineer chooses.
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pick_session import claude_user_text, redact  # noqa: E402

WIN = os.name == "nt"
HOME = Path.home()
STOP = set("a an the to of in on for and or with from by at is are be this that it as into add adds added fix fixes fixed update updates feat chore refactor".split())


# ---------- paths per OS ----------
def data_dir():
    if WIN:
        return Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share"))


def devin_dbs():
    for product in ("cli", "cli-insiders", "cli-dev", "cli-next"):
        p = data_dir() / "devin" / product / "sessions.db"
        if p.exists():
            yield p


def claude_projects_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser() / "projects"


def codex_home():
    return Path(os.environ.get("CODEX_HOME", HOME / ".codex")).expanduser()


def opencode_dbs():
    base = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / "opencode"
    for p in sorted(base.glob("opencode*.db")):
        yield p


# ---------- helpers ----------
def norm(p):
    try:
        s = str(Path(p).expanduser().resolve())
    except Exception:
        s = str(p)
    s = s.rstrip("/\\")
    if WIN or sys.platform == "darwin":
        s = s.lower()
    return s


def inside(repo, d):
    d, repo = norm(d), norm(repo)
    return d == repo or d.startswith(repo + os.sep)


def ro_sqlite(path):
    uri = f"file:{Path(path).as_posix()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def columns(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def to_unix(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else v
    s = str(v)
    if s.isdigit():
        return to_unix(int(s))
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fmt(ts):
    if not ts:
        return "?"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def text_of_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "input_text") and b.get("text"):
                out.append(b["text"])
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return ""


def tokens(s):
    return {t for t in re.findall(r"[a-z0-9_]{3,}", (s or "").lower()) if t not in STOP}


# ---------- readers, each yields dicts with the same keys ----------
def read_devin(repo):
    for db in devin_dbs():
        try:
            conn = ro_sqlite(db)
            cols = columns(conn, "sessions")
            if not cols:
                continue
            hidden = "hidden" in cols
            rows = conn.execute(
                f"SELECT id, working_directory, {'workspace_dirs' if 'workspace_dirs' in cols else 'NULL'}, created_at, last_activity_at, title FROM sessions"
                + (" WHERE hidden = 0" if hidden else "")
            ).fetchall()
            t = tables(conn)
            for sid, wd, wdirs, created, last, title in rows:
                dirs = [wd] if wd else []
                try:
                    dirs += json.loads(wdirs) if wdirs else []
                except Exception:
                    pass
                if not any(inside(repo, d) for d in dirs if d):
                    continue
                first = ""
                try:
                    if "message_nodes" in t:
                        for (cm,) in conn.execute(
                            "SELECT chat_message FROM message_nodes WHERE session_id = ? ORDER BY created_at LIMIT 200", (sid,)
                        ):
                            m = json.loads(cm)
                            if m.get("role") == "user":
                                first = text_of_content(m.get("content"))
                                if first.strip():
                                    break
                    elif "messages" in t:
                        r = conn.execute(
                            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY created_at LIMIT 1", (sid,)
                        ).fetchone()
                        first = r[0] if r else ""
                except sqlite3.Error:
                    pass
                yield {
                    "store": "devin-cli", "store_path": str(db), "session_id": sid, "directory": wd,
                    "created": to_unix(created), "last": to_unix(last), "branch": None,
                    "title": title, "first": first, "text": first,
                }
            conn.close()
        except sqlite3.Error as e:
            print(f"  skip {db}: {e}", file=sys.stderr)


def read_claude(repo):
    pdir = claude_projects_dir()
    if not pdir.exists():
        return
    for f in pdir.glob("*/*.jsonl"):
        if "subagents" in f.parts:
            continue
        cwd = branch = first = None
        created = None
        sid = f.stem
        texts = []
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 400:
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = cwd or rec.get("cwd")
                    branch = branch or rec.get("gitBranch")
                    created = created or to_unix(rec.get("timestamp"))
                    txt = claude_user_text(rec)
                    if txt:
                        texts.append(txt)
                        first = first or txt
        except OSError:
            continue
        if not cwd or not inside(repo, cwd):
            continue
        yield {
            "store": "claude-code", "store_path": str(f), "session_id": sid, "directory": cwd,
            "created": created, "last": f.stat().st_mtime, "branch": branch,
            "title": None, "first": first or "", "text": "\n".join(texts),
        }


def read_codex(repo):
    home = codex_home()
    if not home.exists():
        return
    dbs = sorted(home.glob("state_*.sqlite"), key=lambda p: int(re.findall(r"\d+", p.stem)[-1]))
    seen = set()
    if dbs:
        try:
            conn = ro_sqlite(dbs[-1])
            cols = columns(conn, "threads")
            if cols:
                sel = ["id", "rollout_path", "cwd", "created_at", "updated_at", "title"]
                sel += ["git_branch" if "git_branch" in cols else "NULL"]
                sel += ["first_user_message" if "first_user_message" in cols else "NULL"]
                for tid, rp, cwd, created, updated, title, br, first in conn.execute(f"SELECT {', '.join(sel)} FROM threads"):
                    if not cwd or not inside(repo, cwd):
                        continue
                    seen.add(rp)
                    yield {
                        "store": "codex", "store_path": rp, "session_id": tid, "directory": cwd,
                        "created": to_unix(created), "last": to_unix(updated), "branch": br,
                        "title": title, "first": first or "", "text": first or "",
                    }
            conn.close()
        except sqlite3.Error as e:
            print(f"  skip {dbs[-1]}: {e}", file=sys.stderr)
    # rollouts not in the index, or no index at all
    for f in list(home.glob("sessions/*/*/*/rollout-*.jsonl")) + list(home.glob("archived_sessions/**/rollout-*.jsonl")):
        if str(f) in seen:
            continue
        meta = None
        first = None
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 300:
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") == "session_meta":
                        meta = rec.get("payload") or {}
                    elif rec.get("type") == "response_item":
                        p = rec.get("payload") or {}
                        if p.get("type") == "message" and p.get("role") == "user":
                            txt = text_of_content(p.get("content")).strip()
                            if txt and not txt.startswith("<"):
                                first = txt
                                break
        except OSError:
            continue
        if not meta or not inside(repo, meta.get("cwd", "")):
            continue
        yield {
            "store": "codex", "store_path": str(f), "session_id": meta.get("id") or f.stem, "directory": meta.get("cwd"),
            "created": to_unix(meta.get("timestamp")), "last": f.stat().st_mtime,
            "branch": (meta.get("git") or {}).get("branch"), "title": None, "first": first or "", "text": first or "",
        }
    for z in home.glob("sessions/*/*/*/rollout-*.jsonl.zst"):
        print(f"  note, compressed Codex rollout skipped (needs zstd): {z}", file=sys.stderr)


def read_opencode(repo):
    for db in opencode_dbs():
        try:
            conn = ro_sqlite(db)
            if "session" not in tables(conn):
                continue
            cols = columns(conn, "session")
            parent = "parent_id" if "parent_id" in cols else "NULL"
            for sid, d, title, tc, tu, pid in conn.execute(
                f"SELECT id, directory, title, time_created, time_updated, {parent} FROM session"
            ):
                if pid or not d or not inside(repo, d):
                    continue
                first = ""
                texts = []
                try:
                    for (data,) in conn.execute(
                        "SELECT p.data FROM part p JOIN message m ON m.id = p.message_id WHERE p.session_id = ? ORDER BY m.time_created, p.id LIMIT 400", (sid,)
                    ) if "time_created" in columns(conn, "message") else conn.execute(
                        "SELECT p.data FROM part p JOIN message m ON m.id = p.message_id WHERE p.session_id = ? AND json_extract(m.data, '$.role') = 'user' ORDER BY p.id LIMIT 400", (sid,)
                    ):
                        pd = json.loads(data)
                        if pd.get("type") == "text" and pd.get("text"):
                            texts.append(pd["text"])
                except sqlite3.Error:
                    pass
                first = texts[0] if texts else ""
                yield {
                    "store": "opencode", "store_path": str(db), "session_id": sid, "directory": d,
                    "created": to_unix(tc), "last": to_unix(tu), "branch": None,
                    "title": title, "first": first, "text": "\n".join(texts),
                }
            conn.close()
        except sqlite3.Error as e:
            print(f"  skip {db}: {e}", file=sys.stderr)
    legacy = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / "opencode" / "storage" / "session"
    if legacy.exists() and not list(opencode_dbs()):
        for f in legacy.glob("*/*.json"):
            try:
                s = json.loads(f.read_text())
            except Exception:
                continue
            d = s.get("directory")
            if not d or not inside(repo, d):
                continue
            t = s.get("time") or {}
            yield {
                "store": "opencode", "store_path": str(f), "session_id": s.get("id") or f.stem, "directory": d,
                "created": to_unix(t.get("created")), "last": to_unix(t.get("updated")), "branch": None,
                "title": s.get("title"), "first": "", "text": "",
            }


# ---------- ranking ----------
EVAL_TOOLING = re.compile(r"eval-evaluator|eval-builder|pr_info\.py|find_session\.py|pick_session\.py|EVAL_HOME|hidden-test|frozen task|suitability\.md", re.I)
CLOSE_CALL = 10   # points. Two candidates this close are a question for the engineer, not a ranking


def set_aside(c, pr):
    """Why a session cannot be the one that wrote the PR, or None."""
    created = c.get("created") or c.get("last") or 0
    if created > pr["window_end_unix"] + 600:
        return "started after the PR merged"
    if EVAL_TOOLING.search(c.get("text") or "") or EVAL_TOOLING.search(c.get("title") or ""):
        return "reads like the evaluation itself, not the work"
    return None


def score(c, pr):
    """Points for a session. Time and content weigh more than the branch name, which a later session
    on the same branch (a review, a follow up, this evaluation) shares just as well."""
    s = 0
    reasons = []
    branch = (pr.get("head_ref") or "").lower()
    text = (c.get("text") or "").lower()
    first_commit = pr["window_start_unix"] + 24 * 3600
    merged = pr["window_end_unix"]
    created = c.get("created") or c.get("last")
    last = c.get("last") or created
    if created:
        dist = abs(first_commit - created)
        pts = max(0, 25 - round(25 * dist / (3 * 86400)))
        if pts:
            s += pts
            reasons.append(f"started {dist / 3600:.0f}h from the first commit")
        if last and min(last, merged) - max(created, first_commit) > 0:
            s += 5
            reasons.append("active while the PR grew")
    hits = 0
    for f in pr.get("files") or []:
        base = f.rsplit("/", 1)[-1].lower()
        if base and base in text:
            hits += 1
    if hits:
        s += min(20, 5 * hits)
        reasons.append(f"{hits} touched file names")
    title_t = tokens(pr.get("title"))
    first_t = tokens(c.get("first")) | tokens(c.get("title"))
    if title_t:
        ov = len(title_t & first_t) / len(title_t)
        if ov:
            s += round(20 * ov)
            reasons.append(f"title overlap {ov:.0%}")
    if c.get("branch") and c["branch"].lower() == branch:
        s += 15
        reasons.append("branch match")
    if branch and branch in text:
        s += 10
        reasons.append("branch named in text")
    for i in pr.get("linked_issues") or []:
        if i.get("number") and (f"#{i['number']}" in text or (i.get("url") or "").lower() in text):
            s += 10
            reasons.append("linked issue named")
            break
    return s, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--widen-days", type=float, default=0)
    ap.add_argument("--limit", type=int, default=5)
    a = ap.parse_args()
    task = Path(a.task_dir)
    pr = json.loads((task / "pr.json").read_text())
    bm_path = task / "build-machine.json"
    bm = json.loads(bm_path.read_text()) if bm_path.is_file() else {}
    repo = bm.get("repo_path") or pr.get("repo_path")
    if not repo:
        sys.exit("build-machine.json names no clone. Run pr_info.py again from inside a clone of the repo on this machine.")
    w_start = pr["window_start_unix"] - a.widen_days * 86400
    w_end = pr["window_end_unix"] + a.widen_days * 86400

    if "app.devin.ai/sessions/" in (pr.get("body") or "") or "devinenterprise.com/sessions/" in (pr.get("body") or ""):
        m = re.search(r"https://[\w.-]+/sessions/([0-9a-f]+)", pr["body"])
        print(f"PR body links a Devin cloud session ({m.group(0) if m else '?'}). There is no local log.")
        print("Run  pick_session.py --task-dir ... --devin-cloud <that url>  to pull its messages through the v3 API.")

    print("Searching stores")
    found, aside, filtered = [], [], {"outside window": 0}
    for reader in (read_devin, read_claude, read_codex, read_opencode):
        n = 0
        for c in reader(repo):
            n += 1
            last = c.get("last") or c.get("created") or 0
            created = c.get("created") or last
            if not (w_start <= last and created <= w_end):
                filtered["outside window"] += 1
                continue
            why = set_aside(c, pr)
            if why:
                aside.append({"store": c["store"], "session_id": c["session_id"], "created": fmt(created), "reason": why})
                continue
            c["score"], c["reasons"] = score(c, pr)
            found.append(c)
        print(f"  {reader.__name__[5:]:11s} {n} sessions in this repo")
    found.sort(key=lambda c: (-c["score"], -(c.get("last") or 0)))
    top = found[:a.limit]

    print(f"\n{len(found)} sessions overlap the PR window ({fmt(w_start)} to {fmt(w_end)}), {filtered['outside window']} in this repo were outside it.")
    for x in aside:
        print(f"  set aside  {x['store']}  {x['session_id']}  {x['reason']}")
    if not found:
        print("Nothing found. Options, widen with --widen-days 2, paste a Cursor export with pick_session.py --manual FILE, or use --pr-fallback.")
    close = len(found) > 1 and found[0]["score"] - found[1]["score"] < CLOSE_CALL
    if close:
        print(f"AMBIGUOUS, the top two are within {CLOSE_CALL} points. Show the engineer both first messages and ask which is theirs, the score does not decide this.")
    for i, c in enumerate(top, 1):
        first, _ = redact(re.sub(r"\s+", " ", c.get("first") or "").strip()[:300])
        c["first"] = first
        if c.get("title"):
            c["title"], _ = redact(str(c["title"])[:200])
        print(f"\n[{i}] score {c['score']:3d}  {c['store']}  {c['session_id']}")
        print(f"    {fmt(c.get('created'))} to {fmt(c.get('last'))}  branch {c.get('branch') or '?'}  {', '.join(c['reasons']) or 'directory and time only'}")
        print(f"    first message: {first or '(none captured)'}")
    for c in top:
        c.pop("text", None)
    (task / "candidates.json").write_text(json.dumps({
        "window_start_unix": w_start, "window_end_unix": w_end, "shown": len(top), "overlapping": len(found),
        "ambiguous": close, "set_aside": aside, "candidates": top,
        "note": "this machine only, store paths inside, never frozen, never shipped",
    }, indent=2))
    print(f"\nwrote {task / 'candidates.json'}. Ask the engineer which number is theirs, then run pick_session.py --pick N.")


if __name__ == "__main__":
    main()
