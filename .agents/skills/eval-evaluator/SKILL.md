---
name: eval-evaluator
description: Turn a merged pull request into the start of an eval. Pulls the PR, decides whether it is a good eval candidate with a written reason, finds the engineer's own agent session behind the PR on this laptop (Devin CLI, Claude Code, Codex, OpenCode) and saves a redacted excerpt of the human turns as the original ask. Use when someone says "evaluate PR 123 for an eval", "is this PR a good eval", or "find the session behind PR 123".
---

# eval-evaluator

You run inside the engineer's clone of the repository. You write into one eval folder,
`$EVAL_HOME` (default `~/evals`), one subfolder per PR named `<repo>-pr-<number>`.
Everything you write there will later be zipped and sent to Cognition, so never copy raw
transcripts, tokens or environment values into it. Scripts live next to this file in `scripts/`.

Talk to the engineer in short plain sentences. Ask one question at a time. The engineer
decides, you recommend and record.

## Step 1, pull the PR

```
python3 scripts/pr_info.py <number> [--repo-path .] [--evals $EVAL_HOME]
```

It needs `gh` logged in. It writes `pr.json` and prints a worksheet. Read the worksheet and
tell the engineer one line, for example "PR 4821, add retry to webhook delivery, 6 files,
merged Sep 12". If the script says the PR is not merged, stop, only merged PRs become evals.

`pr.json` carries `repo_url`, the https address of the repository that any laptop can fetch
from, taken from `gh` or rebuilt from the remote. Never a file path, never an ssh remote,
never a git proxy address from the machine you run on. The path of the clone you are in goes
to `build-machine.json`, a note for this machine only that is never frozen or shipped.

## Step 2, decide suitability, tests 1 and 2

Read the diff (`gh pr diff <number>`) and the PR body. Apply the three tests below, but only
1 and 2 now, 3 needs the session and comes in step 5.

Test 1, checkable behaviour change. Write one sentence of the form
"Call X with Y. Before the PR you get A. After the PR you get B." If you can write it the
PR passes and the sentence later seeds the hidden test. You cannot write it for docs only,
formatting, lint fixes, generated files, styling with no observable output, or a config
value with no observable effect, those are a no. Frontend changes pass only if the
engineer can name a DOM or API assertion.

Test 2, real work not mechanical. A no when the diff is only lockfiles or version fields,
when the title starts with bump, revert, rename or merge, when it is a squash of someone
else's branch, or when the change is a search and replace. A warning, not a no, when it
touches more than about 30 files or several unrelated areas, a single one shot prompt will
not describe it well. The worksheet from step 1 flags most of these, confirm against the diff.

Say your verdict and reason in two or three sentences and ask "Go ahead with this one?"
If the engineer overrides you either way, record their reason. Write
`suitability.md` from `../eval-builder/templates/suitability.md` with what you know so far.
The first line must be `Verdict yes` or `Verdict no`. On a no you are done, leave the folder
in place so the no is recorded.

This question is the first of three gates. Wait for the answer, then record it in the
engineer's own words

```
python3 ../eval-builder/scripts/gate.py --task-dir DIR --gate suitability --by "<name>" --said "<their words>"
```

An instruction given once at the start, "evaluate, build and freeze it", is not a yes at
this gate. Ask here, and the builder asks again before freezing. `freeze.py` refuses a
task that lacks the recorded gates.

## Step 3, find the session

```
python3 scripts/find_session.py --task-dir $EVAL_HOME/<task-id>
```

It searches only the known stores for this OS, keeps sessions whose working directory is
this repo and whose time overlaps the PR window (six hours before the first commit to the
merge), ranks by time, PR title words, touched files and branch, and prints the top
candidates with their first message. It writes `candidates.json` and never picks for you.
It sets aside sessions that started after the merge and sessions that read like this
evaluation itself (your own session on the same branch is the usual one) and lists them
under `set_aside` with the reason. When the top two are within ten points it prints a
warning and marks `ambiguous` true. In that case show both first messages side by side and
ask which one is theirs, never assume the first.

Show the engineer the list and ask "Which of these is yours, or none?"

- Nothing found, rerun with `--widen-days 2`. Claude Code deletes transcripts after
  30 days by default, Codex and Devin CLI keep theirs, say so if the PR is old.
- The PR body links a Devin cloud session, there is no local log. Ask for `DEVIN_API_KEY`
  and `DEVIN_ORG_ID` in the environment and use `--devin-cloud <url>` in step 4.
- Cursor or another tool with no supported store, ask the engineer to export the chat to a
  file and use `--manual FILE` in step 4.
- Still nothing, use `--pr-fallback` in step 4. The prompt then comes from the PR body,
  linked issues and commit messages and is marked as such.

## Step 4, save the original ask

Exactly one of

```
python3 scripts/pick_session.py --task-dir DIR --pick N
python3 scripts/pick_session.py --task-dir DIR --manual exported-chat.md
python3 scripts/pick_session.py --task-dir DIR --devin-cloud https://app.devin.ai/sessions/<id>
python3 scripts/pick_session.py --task-dir DIR --pr-fallback
```

It parses only the picked session, keeps the human turns, drops assistant text and tool
output, drops turns that repeat an earlier turn word for word (the Devin CLI store can
repeat a turn), redacts credential shaped text and emails, and writes
`session-excerpt.md` plus `session-source.json` with the store name and the SHA-256 of the
raw source. For Claude Code it also keeps a turn typed while Claude was still working, which
the store files as a queued command rather than a user message. The path of the raw store
goes to `build-machine.json`, never into the shipped record. If the store gives every turn
the same time, the excerpt numbers the turns and says so rather than printing a time that is
not real. The raw session stays where it was. Do not open the raw store yourself and paste
from it.

## Step 5, test 3, self contained

Read `session-excerpt.md` and the PR body. Look for anything the original work needed from
outside the repo, a ticket, an error report, a dashboard, a database row, a Slack thread,
an MCP or curl to an internal service. The excerpt only has human turns, so also ask the
engineer "Did your session read from Sentry, Linear, Slack, a database or any MCP while
doing this?" For each source found ask them to paste what was actually used. It goes into
the prompt's context section in eval-builder, note it in `suitability.md` under test 3 now.
If they cannot or will not paste it, the PR is a no, change the verdict line and the reason.

GitHub used only at the end, pushing, opening the PR, watching CI, fixing review comments,
does not count. A lookup that was tried and did not help does not count either.

## Step 6, hand off

Finish `suitability.md` with the engineer's decision, name and date. Then record the
suitability gate once more with the words they said at step 2, so its hashes cover the
finished file (the gate covers `pr.json` and `suitability.md`, and `freeze.py` refuses a
file edited after the yes it recorded). This is not a new question unless the verdict
changed at step 5, in which case ask again.

```
python3 ../eval-builder/scripts/gate.py --task-dir DIR --gate suitability --by "<name>" --said "<their words from step 2>"
```

Tell the engineer the
folder is ready for eval-builder in one line. The folder now has `pr.json`,
`suitability.md`, `gates.json`, `candidates.json`, `session-excerpt.md`,
`session-source.json` and `build-machine.json` (this machine's notes, never shipped).

## What you never do

- Pick a session for the engineer, even when one candidate is obvious.
- Record a gate the engineer did not answer at that gate.
- Write a file path from this machine into `pr.json`, `session-source.json` or any other
  file that ships.
- Write transcript text, tokens, emails or environment values into the eval folder.
- Install anything into the repository or change files in it.
- Skip the written reason on a no.
