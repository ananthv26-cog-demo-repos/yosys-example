#!/usr/bin/env bash
# Hidden test for <task-id>. Lives in the eval folder, never in the repo.
# Runs with the fresh checkout as cwd. EVAL_REPO, EVAL_BASE_SHA and EVAL_TASK_DIR are set.
#   exit 0  behaviour present
#   exit 1  behaviour absent
#   exit 2+ infrastructure problem (setup missing, service down), never counted as a fail
#
# Rules. Test public behaviour, call the code the way a user or caller would.
# No grep over source, no AST inspection, no mocking the code under test.
# Any name the PR introduced that this test uses (a function, parameter, flag,
# key, message) must be listed under ## Interface in prompt.md, the leak check
# refuses one that is not.
set -u

# 1. Preflight, anything missing here is infra, exit 2.
command -v python3 >/dev/null || { echo "python3 missing"; exit 2; }

# 2. Drive the behaviour and capture the result.
#    Prefer a small script next to this file (probe.py, probe.mjs, probe.sh) over an inline one liner.
#    Example
# out=$(python3 "$EVAL_TASK_DIR/hidden-test/probe.py" 2>&1); rc=$?
# if [ $rc -ge 2 ]; then echo "$out"; exit 2; fi

# 3. Decide. Print what you saw so the proof files are readable.
# if echo "$out" | grep -q "expected marker"; then echo "PASS $out"; exit 0; else echo "FAIL $out"; exit 1; fi
echo "test.sh not written yet"
exit 2
