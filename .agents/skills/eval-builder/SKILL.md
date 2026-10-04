---
name: eval-builder
description: Build a frozen, tool agnostic eval from a PR that eval-evaluator already approved. Drafts the one shot prompt from the engineer's original ask, pins the base commit and proves the setup on a fresh clone, writes a standalone hidden test that fails on base and passes on the merge, drafts the would merge criteria, checks the prompt for leaks, and freezes everything with the engineer's approval. Use when someone says "build the eval for PR 123" or "freeze this eval".
---

# eval-builder

Input is an eval folder from eval-evaluator, `$EVAL_HOME/<repo>-pr-<number>`, with
`pr.json`, `suitability.md` (first line `Verdict yes`), `gates.json` with the suitability
gate, `session-excerpt.md` and `session-source.json`. You run inside the engineer's clone,
or pass `--repo-path` to a clone that has the PR's commits. Scripts are in `scripts/`,
templates in `templates/`. Nothing you write may name the PR, its branch, its commits, its
files or its solution, except inside `pr.json` and `setup.json` which the runner never shows
the agent, and the names the hidden test needs, which go under `## Interface` in the prompt.

Ask one question at a time. Expect most of the engineer's time in steps 1 and 3.

## Step 1, the one shot prompt

Draft `prompt.md` from the human turns in `session-excerpt.md`. Write it as the engineer
would have typed it to an agent on day one, in their words, one message. Rules

- Describe the symptom or the need, not the root cause and not the fix.
- No PR number, branch name, commit sha, file names the PR created, or test names.
- No commands to run, no patch hunks, no "add a function called" outside `## Interface`.
- Include a section `## Context` holding everything the original session fetched from
  outside the repo, pasted in full by the engineer in eval-evaluator step 5. Ticket text,
  stack traces, schema, sample rows. Nothing from after the PR was opened.
- If the hidden test from step 3 uses anything the PR introduced, a function or method name,
  a parameter or keyword, a CLI flag, a config key, a header name, a string key, an exact
  message or any other exact value an attempt could only match by chance, add a section
  `## Interface` that lists each such name in backticks with its signature or default, one
  line each, and nothing about how it works. The leak check applies the same rule to every
  word, a standard header name counts as new when this repository did not use it before, so
  list it or let the test accept any name. That is the only place the prompt names anything
  the PR chose. Come back and add
  it after step 3.
- If the ask came from `--pr-fallback`, say so in one line at the top of the file so
  Cognition knows the prompt was reconstructed.

Ask the engineer to edit it until it reads like what they actually asked. Then

```
python3 scripts/leak_check.py --task-dir DIR
```

Before step 2 there is no `setup.json`, so this first run checks the prompt text only and
says so. Run it again after `prove.py setup` for the checks against the diff, and once more
when the hidden test exists.

It flags the PR number, branch, shas, PR and issue URLs, files the PR created, hidden test
references, solution phrases, identifiers the diff introduced and six word overlaps with
the diff, the last two outside `## Interface` only. Once the hidden test exists it also
fails when the test uses a name the PR introduced that `## Interface` does not list, and
warns about Interface names the test does not use. Hard hits fail the run and must be
fixed. Soft hits, explain them to the engineer and let them decide. It writes
`leak-check.json`.

## Step 2, the base commit and setup

```
python3 scripts/base_commit.py --task-dir DIR
```

It computes merge-base(PR head, merge commit^) and the parent of the first PR commit,
refuses when they disagree, and writes `setup.json` with `repo_url`, `base_sha`,
`merge_sha`, `head_sha` and empty `setup_commands`, `env_names`, `services`. On a
disagreement show both shas to the engineer, ask which tree they started from, and rerun
with `--base <full sha> --reason "<why>"`.

Fill `setup.json`. Read the README and the CI config at the base commit for the install
commands. `setup_commands` is the list of shell commands that make a fresh clone at base
work, run in order in the checkout. `env_names` is the names only of environment variables
those commands or the code need at runtime. `services` is the names of external services
the repo talks to. Never write a value. Then

```
python3 scripts/prove.py setup --task-dir DIR
```

It runs the commands on a fresh tree at base in a temp folder and writes `setup-proof.txt`.
If it fails, ask "What do you run to get this repo working from a clean clone?" and
"Which env vars or services does it need, names only?" Fix `setup.json` and rerun until it
passes. Do not fix the repository to make setup pass.

## Step 3, the hidden test

Copy `templates/test.sh` to `hidden-test/test.sh`. Write the test from the test 1 sentence
in `suitability.md`. It runs with the fresh checkout as cwd and `EVAL_REPO`,
`EVAL_BASE_SHA`, `EVAL_TASK_DIR` set. Exit 0 when the behaviour is present, 1 when absent,
2 or higher for anything infrastructural (missing tool, service down), which is never
counted as a fail. Put helper scripts next to it in `hidden-test/`.

Rules for the test. Drive public behaviour the way a caller or user would. No grep or
find over the source, no opening a source file, no AST or `__file__` inspection, no `git`
command of any kind, no mocking the code under test, no comparison against `HEAD`, use
`EVAL_BASE_SHA` if you need the base. Freeze refuses a test that does any of these and
names the line, its patterns have gaps, so the engineer reads the test at the proof gate.
The test must not need a live third party service. If
the repo has no test suite that is fine, the test is standalone. When the test has to call
a name the real PR chose, such as a new keyword argument, the prompt must state it. Add it
to `## Interface` in `prompt.md` rather than hoping the attempt picks the same name.

If you cannot write it, ask "How would you check by hand that this PR works?" and turn the
answer into a script. Optionally add `hidden-test/wrong-fix.patch`, a plausible wrong
patch against base that the test must reject, the engineer names it. Then

```
python3 scripts/prove.py hidden-test --task-dir DIR
```

It runs the test on a fresh tree at base and at the merge commit, and on the wrong fix if
present, and writes `hidden-test/proof.json` and `hidden-test/proof/*.txt`. That folder
holds the two control outputs only, a helper the test needs goes beside `test.sh`, freeze
refuses code under `proof/`. It refuses
when base passes, that is not a test. It refuses when merge fails. Fix the test, not the
verdict. Show the engineer both outputs and ask them to read the test. Then run
`leak_check.py` again, it now checks the test against the prompt.

Then the second gate. Ask "Setup and the hidden test are proven, both outputs above. Do you
accept them?" Wait for the answer and record it

```
python3 scripts/gate.py --task-dir DIR --gate proof --by "<name>" --said "<their words>"
```

`--by` is who approved. When the sentence was yours, a draft you offered and they accepted,
add `--said-by "<your name>"`. When you ran the command instead of the engineer add
`--recorded-by "<your name>"`, the default records the agent. The record must never put
your words in their mouth, this applies to all three gates.

## Step 4, the grading criteria

Copy `templates/criteria.md` to `criteria.md` and fill it from the diff and the review
comments on the PR. Blocking items are the requirements in the user's words, each naming
the plausible wrong answer it rules out and ending with its source, `Source, the ask` or
`Source, existing behaviour`, freeze refuses a blocking line without one. A fact that exists
only in the PR, the name it chose, a file it added, how it did it, is never blocking, it goes
under Advisory or Not required. Advisory items do not block. List accepted
alternatives that differ from the real PR but should pass, and what the real PR did that
an attempt need not do. The engineer edits in place.

## Step 5, the isolation rules

Copy `templates/isolation.md` to `isolation.md`. The runner prepends it to every prompt.
Show it to the engineer, they may add a line, they should not weaken one.

## Step 6, approve and freeze

Print the folder listing and ask "Approve and freeze?" This is the third gate. On yes

```
python3 scripts/gate.py --task-dir DIR --gate freeze --by "<name>" --said "<their words>"
python3 scripts/freeze.py --task-dir DIR --engineer "<name>"
```

`freeze.py` refuses when any of the three gates (suitability, proof, freeze) is missing
from `gates.json`, and when a file a gate covered changed after that yes (`gate.py` records
their hashes beside the words), in which case ask again and record that gate anew. Only when the engineer has said in so many words that they do not want
to be asked at each gate, run it with `--one-shot "<their words>"` instead of the three
`gate.py` calls. The reason is written into `approval.json` where Cognition reads it. Never
decide that yourself.

It checks the folder against the contract in `scripts/contract.py` (every artifact present,
required fields, a full 40 character base sha, suitability yes, setup and hidden test proofs
ok, a session excerpt or a source record with `prompt_source`), lints every file under
`hidden-test/` for source or git inspection, with paths held in variables, arrays,
indirect and default expansions, `printf -v`, `read` and `mapfile` resolved in statement order, literals
joined with `+` or `join` folded, the shell inside `eval`, backticks, subprocess strings
and argument arrays read, copies, links, archives, listings and file URLs of the tree
counted, a function's text through `toString` or string conversion and a private name of
the product (leading underscore) refused, the repository's own `tests/` and `fixtures/`
material left readable, refuses code under `hidden-test/proof/`, runs the
leak check once more on the final prompt and hidden test and refuses on a hard hit. A
refusal writes `rejected.json` with the reasons. A flagged line the engineer has read and
judges fine ends with `# lint ok, <their reason>`, a `#` comment, `//` only in a JavaScript
helper, the reason in words, freeze then records the line, every rule it tripped and the reason
under `lint_acknowledged` in `approval.json`, hashed so an edit to that list or its removal after
the freeze is refused, and `evals check` shows them.
Write the reason in the engineer's words, never add the marker on your own. On success it
writes `approval.json` with the engineer's name, the time, `build_path` (local or
devin-cloud, detected from the environment or set with `--build-path`) and the SHA-256 of
every contract file. `build_path` is metadata for the results table only, nothing else about
the build machine is recorded, the OS and Python of the setup proof stay in `setup.json`.

The same engineer owns this eval from here, runs the attempts on their laptop and grades
them. Tell them the eval is frozen and that `evals prove <task>` then `evals run <task>`
come next, one line. When you built it in a Devin cloud session, run
`python3 scripts/zip_task.py --task-dir <task>` from this skill's folder and attach the zip,
or commit the folder to the team's task repository. The script refuses to zip while
`build-machine.json` or `candidates.json` is still in the folder, both name this machine's
folders and sessions and stay behind, delete them first.

## What you never do

- Put the PR number, branch, sha, PR file names or the fix into `prompt.md`. Only the names
  the hidden test needs go under `## Interface`, never how to build them.
- Write environment variable values or credentials anywhere in the folder.
- Weaken the hidden test or the proof to get a pass.
- Change the repository.
- Edit a frozen file. If something must change, delete `approval.json`, change it, and
  freeze again with the engineer.
- Record a gate the engineer did not answer at that gate, or pass `--one-shot` on your own.
- Write a path from this machine into any file that ships. `build-machine.json` is the one
  place for the clone path, `candidates.json` names sessions on this machine, both stay behind.
