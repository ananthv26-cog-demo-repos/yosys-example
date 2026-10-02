#!/usr/bin/env python3
"""Parse the session the engineer picked, keep the human turns, redact, hash.

Usage
  pick_session.py --task-dir DIR --pick N              a number from candidates.json
  pick_session.py --task-dir DIR --manual FILE         a chat the engineer exported by hand (Cursor)
  pick_session.py --task-dir DIR --devin-cloud URL     a Devin cloud session, read through the v3 API
  pick_session.py --task-dir DIR --pr-fallback         nothing found, seed from the PR text

Writes session-excerpt.md (ships) and session-source.json (ships, no paths). The raw
transcript never leaves the laptop, and the path to it goes in build-machine.json, which
is neither frozen nor shipped.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

REDACTIONS = [
    ("github token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("slack token", re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}\b")),
    ("aws key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("secret assignment", re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[=:]\s*['\"]?[^\s'\"]{6,}")),
    ("url credentials", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@[^\s]+")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("long hex", re.compile(r"\b[a-f0-9]{40,}\b")),
    ("long base64", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9+/])")),
]


PROMPT_SOURCE = {"pr-fallback": "pr_body_linked_issues_commit_messages", "manual": "exported_chat", "devin-cloud": "devin_api_user_messages"}


def redact(text):
    count = 0
    for kind, rx in REDACTIONS:
        text, n = rx.subn(f"[REDACTED {kind}]", text)
        count += n
    return text, count


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_rows(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def ro_sqlite(path):
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro&immutable=1", uri=True)


def text_of_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content
            if isinstance(b, dict) and b.get("type") in ("text", "input_text") and b.get("text")
        )
    return ""


def claude_user_text(rec):
    """The human text in one Claude Code jsonl record, or None.

    A turn typed while Claude was still working is stored as an attachment record of type
    queued_command, not as a user record, so both shapes count. Sidechain and meta records,
    tool results and injected system text do not.
    """
    if rec.get("isSidechain") or rec.get("isMeta"):
        return None
    if rec.get("type") == "attachment":
        att = rec.get("attachment") or {}
        if att.get("type") == "queued_command":
            return (att.get("prompt") or "").strip() or None
        return None
    if rec.get("type") != "user":
        return None
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    txt = text_of_content(content).strip()
    if not txt or txt.startswith("<"):
        return None
    return txt


def note_build_machine(task, **fields):
    """Merge fields into build-machine.json, this machine's own notes. Never frozen, never shipped."""
    p = Path(task) / "build-machine.json"
    data = json.loads(p.read_text()) if p.is_file() else {}
    data.update(fields)
    p.write_text(json.dumps(data, indent=2) + "\n")


def ts(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        v = v / 1000 if v > 1e11 else v
        return dt.datetime.fromtimestamp(v).isoformat(timespec="seconds")
    return str(v)[:19]


# ---------- extractors, each returns (turns[(timestamp, text)], sha256) ----------
def from_devin(c):
    conn = ro_sqlite(c["store_path"])
    rows = []
    turns = []
    t = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "message_nodes" in t:
        for nid, cm, created in conn.execute(
            "SELECT node_id, chat_message, created_at FROM message_nodes WHERE session_id = ? ORDER BY created_at, node_id", (c["session_id"],)
        ):
            rows.append([nid, cm, created])
            m = json.loads(cm)
            meta = m.get("metadata") or {}
            if m.get("role") == "user" and (meta.get("is_user_input") in (1, True, None)):
                txt = text_of_content(m.get("content")).strip()
                if txt:
                    turns.append((ts(created), txt))
    elif "messages" in t:
        for mid, role, content, created in conn.execute(
            "SELECT id, role, content, created_at FROM messages WHERE session_id = ? ORDER BY created_at", (c["session_id"],)
        ):
            rows.append([mid, role, content, created])
            if role == "user" and content:
                turns.append((ts(created), content))
    conn.close()
    return turns, sha256_rows(rows)


def from_claude(c):
    turns = []
    with open(c["store_path"], encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            txt = claude_user_text(rec)
            if txt:
                turns.append((ts(rec.get("timestamp")), txt))
    return turns, sha256_file(c["store_path"])


CODEX_INJECTED = ("<environment_context>", "<user_instructions>", "<turn_aborted>", "# AGENTS.md", "<permissions")


def from_codex(c):
    turns = []
    p = c["store_path"]
    if p.endswith(".zst"):
        sys.exit("This rollout is zstd compressed. Decompress it first (zstd -d FILE) and point --manual at the result.")
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "response_item":
                continue
            pl = rec.get("payload") or {}
            if pl.get("type") == "message" and pl.get("role") == "user":
                txt = text_of_content(pl.get("content")).strip()
                if txt and not txt.startswith(CODEX_INJECTED):
                    turns.append((ts(rec.get("timestamp")), txt))
    return turns, sha256_file(p)


def from_opencode(c):
    conn = ro_sqlite(c["store_path"])
    rows, turns = [], []
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    order = "m.time_created, p.id" if "time_created" in mcols else "m.id, p.id"
    for mid, mdata, pdata, tc in conn.execute(
        f"SELECT m.id, m.data, p.data, {'m.time_created' if 'time_created' in mcols else 'NULL'} FROM message m JOIN part p ON p.message_id = m.id WHERE m.session_id = ? ORDER BY {order}",
        (c["session_id"],),
    ):
        rows.append([mid, mdata, pdata, tc])
        md, pd = json.loads(mdata), json.loads(pdata)
        if md.get("role") == "user" and pd.get("type") == "text" and pd.get("text", "").strip():
            turns.append((ts(tc or (md.get("time") or {}).get("created")), pd["text"].strip()))
    conn.close()
    return turns, sha256_rows(rows)


def from_devin_cloud(url_or_id):
    key = os.environ.get("DEVIN_API_KEY")
    org = os.environ.get("DEVIN_ORG_ID")
    base = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/")
    if not key or not org:
        sys.exit("Set DEVIN_API_KEY and DEVIN_ORG_ID (your own key, the org id from the Devin settings page).")
    sid = re.search(r"sessions/([0-9a-f-]+)", url_or_id)
    sid = sid.group(1) if sid else url_or_id
    sid = sid if sid.startswith("devin-") else f"devin-{sid}"
    turns, raw = [], []
    after = None
    while True:
        u = f"{base}/v3/organizations/{org}/sessions/{sid}/messages?first=100" + (f"&after={after}" if after else "")
        req = urllib.request.Request(u, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = json.load(r)
        items = page.get("items") if isinstance(page, dict) else page
        raw.extend(items or [])
        for m in items or []:
            if m.get("source") == "user" and m.get("message"):
                turns.append((ts(m.get("created_at")), m["message"]))
        if isinstance(page, dict) and page.get("has_next_page") and page.get("end_cursor"):
            after = page["end_cursor"]
        else:
            break
    return turns, sha256_rows(raw), sid


def dedupe(turns):
    seen, kept = set(), []
    for when, txt in turns:
        key = " ".join(txt.split())
        if key not in seen:
            seen.add(key)
            kept.append((when, txt))
    return kept, len(turns) - len(kept)


def turn_spread(turns):
    try:
        times = [dt.datetime.fromisoformat(when) for when, _ in turns if when]
    except ValueError:
        return None
    if len(times) < 2 or len(times) != len(turns):
        return None
    return (max(times) - min(times)).total_seconds()


def write(task, source, turns, sha, extra, count_redactions=True):
    turns, dropped = dedupe(turns)
    spread = turn_spread(turns)
    same_time = turns[0][0] if spread is not None and spread < 60 else ""
    lines = [f"# Original ask, source {source}", ""]
    if dropped:
        lines += [f"Dropped {dropped} turn{'s' if dropped != 1 else ''} repeating an earlier turn word for word, the store kept them more than once.", ""]
    if same_time:
        lines += [f"The store gives every turn the same time, to within a minute of {same_time}, so turns are numbered in store order and per turn times are unknown.", ""]
    total = 0
    for i, (when, txt) in enumerate(turns, 1):
        red, n = redact(txt)
        total += n
        lines.append(f"## turn {i}" if same_time else f"## {when or 'turn'}")
        lines.append("")
        lines.append(red)
        lines.append("")
    (task / "session-excerpt.md").write_text("\n".join(lines))
    meta = {"source": source, "prompt_source": PROMPT_SOURCE.get(source, "session_human_turns"), "sha256": sha, "turns": len(turns),
            "duplicate_turns_dropped": dropped, "redactions": total,
            "selected_by": "engineer", "picked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if same_time:
        meta["turn_times"] = f"the store gives every turn the same time, to within a minute of {same_time}"
    meta.update(extra)
    (task / "session-source.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {task / 'session-excerpt.md'} ({len(turns)} human turns, {dropped} repeats dropped, {total} redactions) and session-source.json")
    if total:
        print("Read the excerpt and check the redactions did not remove something the prompt needs.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pick", type=int)
    g.add_argument("--manual")
    g.add_argument("--devin-cloud")
    g.add_argument("--pr-fallback", action="store_true")
    a = ap.parse_args()
    task = Path(a.task_dir)
    pr = json.loads((task / "pr.json").read_text())

    if a.pick:
        cands = json.loads((task / "candidates.json").read_text())
        c = cands["candidates"][a.pick - 1]
        turns, sha = {"devin-cli": from_devin, "claude-code": from_claude, "codex": from_codex, "opencode": from_opencode}[c["store"]](c)
        if not turns:
            sys.exit("No human turns found in that session. Pick another or use --pr-fallback.")
        write(task, c["store"], turns, sha, {
            "session_id": c["session_id"], "score": c["score"],
            "candidates_shown": cands["shown"], "window_start_unix": cands["window_start_unix"], "window_end_unix": cands["window_end_unix"],
        })
        note_build_machine(task, store=c["store"], store_path=c["store_path"])
    elif a.manual:
        p = Path(a.manual)
        text = p.read_text(encoding="utf-8", errors="replace")
        write(task, "manual", [("exported chat", text)], sha256_file(p), {})
        note_build_machine(task, store="manual", store_path=str(p))
        print("Manual source. Trim the excerpt to the human turns before drafting the prompt.")
    elif a.devin_cloud:
        turns, sha, sid = from_devin_cloud(a.devin_cloud)
        if not turns:
            sys.exit("No user messages came back. Check the session id and that the key can read it.")
        write(task, "devin-cloud", turns, sha, {"session_id": sid})
    else:
        parts = [("PR body", pr.get("body") or "(empty)")]
        for i in pr.get("linked_issues") or []:
            parts.append((f"linked issue #{i.get('number')} {i.get('title') or ''}", i.get("body") or "(no body)"))
        parts.append(("commit messages", "\n".join(pr.get("commit_messages") or [])))
        write(task, "pr-fallback", parts, sha256_rows(parts), {})
        print("PR fallback. The prompt must be rewritten into the ask the engineer would have typed, PR bodies describe the result not the request.")


if __name__ == "__main__":
    main()
