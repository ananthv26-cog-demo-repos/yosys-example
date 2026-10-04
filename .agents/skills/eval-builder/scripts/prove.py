#!/usr/bin/env python3
"""Prove setup.json and the hidden test on fresh checkouts.

Usage
  prove.py setup       --task-dir DIR            setup commands work on a fresh tree at base
  prove.py hidden-test --task-dir DIR [--keep]   test.sh exits 1 on base and 0 on the merge commit
                                                 (also runs hidden-test/wrong-fix.patch when present, must exit 1)

A fresh checkout is `git archive <sha>` unpacked into a temp folder with a new
single commit, so there is no history and no remote. Every attempt in stage 2
starts from the same kind of tree, so what is proven here is what runs there.

Hidden test contract. hidden-test/test.sh runs with the checkout as cwd and
EVAL_REPO, EVAL_BASE_SHA, EVAL_TASK_DIR in the environment.
  exit 0   behaviour present (pass)
  exit 1   behaviour absent  (fail)
  other    infrastructure problem, never counted as fail
"""
import argparse
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import build_machine  # noqa: E402

# bash -c, not -l. A login shell re-reads the profile, and on macOS path_helper then puts /usr/bin ahead of
# whatever the engineer put first on PATH, so the python they installed was not the one setup ran with.
# The PATH the engineer sees is the PATH setup gets, on the build machine and on every laptop alike.
SHELL = ["bash", "-c"]
SUBMODULE_NOTED = []


def portable(text, task, *checkouts):
    """Machine paths out of the proofs, which are frozen and shipped. Placeholders name the role of the path."""
    pairs = [(str(c), "<checkout>") for c in checkouts if c] + [(str(task), "<task>"), (str(task.parent), "$EVAL_HOME"),
                                                                 (str(Path.home()), "~"), (tempfile.gettempdir(), "<tmp>")]
    for real, placeholder in pairs:
        text = text.replace(real, placeholder)
    return text


def run(cmd, cwd, env=None, log=None, timeout=None):
    t0 = time.time()
    p = subprocess.run(SHELL + [cmd], cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    out = f"$ {cmd}\n{p.stdout}{p.stderr}\n[exit {p.returncode}, {time.time() - t0:.1f}s]\n"
    if log is not None:
        log.append(out)
    return p.returncode, out


def fresh_checkout(repo, sha, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    ar = subprocess.Popen(["git", "archive", "--format=tar", sha], cwd=repo, stdout=subprocess.PIPE)
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=ar.stdout)
    ar.wait()
    if ar.returncode or tar.returncode:
        sys.exit(f"could not export {sha} from {repo}")
    if (dest / ".gitmodules").exists() and not SUBMODULE_NOTED:
        SUBMODULE_NOTED.append(sha)
        print("  note, the repository has submodules and git archive leaves them out of the fresh tree. Nothing to do unless setup or the hidden test needs one, then add a setup command that fetches it")
    g = lambda *args: subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@local", "-c", "commit.gpgsign=false", *args], cwd=dest, check=True, capture_output=True)
    g("init", "-q")
    g("add", "-A")
    g("commit", "-q", "-m", f"base {sha}", "--allow-empty")
    return dest


def source_repo(task, pr, setup, override=None):
    """A git dir that holds base and merge. The clone this machine built from (build-machine.json or
    --repo-path), else a fresh fetch from repo_url, so a folder can be proven on a machine that never had the clone."""
    shas = [setup["base_sha"], setup["merge_sha"]]

    def has(d):
        return d and Path(d).is_dir() and all(
            subprocess.run(["git", "cat-file", "-e", f"{x}^{{commit}}"], cwd=d, capture_output=True).returncode == 0 for x in shas)

    for cand in (override, build_machine(task).get("repo_path"), pr.get("repo_path")):
        if cand and Path(cand).expanduser().is_dir():
            cand = str(Path(cand).expanduser())
            if not has(cand):
                subprocess.run(["git", "fetch", "--quiet", "origin", *shas], cwd=cand, capture_output=True)
            if has(cand):
                return cand
    d = tempfile.mkdtemp(prefix="eval-src-")
    subprocess.run(["git", "init", "-q", "--bare"], cwd=d, check=True)
    subprocess.run(["git", "remote", "add", "origin", setup["repo_url"]], cwd=d, check=True)
    for args in (["fetch", "--quiet", "origin", *shas], ["fetch", "--quiet", "origin"]):
        subprocess.run(["git", *args], cwd=d, capture_output=True)
        if has(d):
            return d
    sys.exit(f"no clone holds {shas[0][:12]} and {shas[1][:12]}, and fetching from {setup['repo_url']} did not bring them. Pass --repo-path to a clone that has them.")


def run_setup(setup, dest, log):
    env = dict(os.environ, EVAL_REPO=str(dest), EVAL_BASE_SHA=setup["base_sha"])
    codes = []
    for cmd in setup.get("setup_commands") or []:
        code, _ = run(cmd, dest, env, log)
        codes.append(code)
        if code != 0:
            break
    return codes


def cmd_setup(a):
    task = Path(a.task_dir)
    setup = json.loads((task / "setup.json").read_text())
    pr = json.loads((task / "pr.json").read_text())
    pr["repo_path"] = source_repo(task, pr, setup, a.repo_path)
    if not setup.get("setup_commands"):
        print("setup_commands is empty. If the repo truly needs nothing, put [\"true\"] there so the proof is explicit.")
        sys.exit(2)
    tmp = Path(tempfile.mkdtemp(prefix="eval-setup-"))
    log = []
    t0 = time.time()
    print(f"fresh checkout of {setup['base_sha'][:12]} in {tmp}")
    fresh_checkout(pr["repo_path"], setup["base_sha"], tmp)
    codes = run_setup(setup, tmp, log)
    ok = bool(codes) and all(c == 0 for c in codes)
    (task / "setup-proof.txt").write_text(portable("".join(log), task, tmp))
    setup["setup_proof"] = {
        "ok": ok, "exit_codes": codes, "seconds": round(time.time() - t0, 1),
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(), "os": platform.platform(), "python": platform.python_version(), "shell": "bash -c",
    }
    (task / "setup.json").write_text(json.dumps(setup, indent=2))
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    print("".join(log)[-3000:])
    print("setup proven on a fresh tree" if ok else "setup FAILED, fix setup_commands and rerun")
    sys.exit(0 if ok else 1)


def run_hidden(task, setup, pr, label, sha, patch=None, keep=False):
    tmp = Path(tempfile.mkdtemp(prefix=f"eval-{label}-"))
    log = [f"== {label} at {sha}{' with ' + str(patch) if patch else ''} ==\n"]
    fresh_checkout(pr["repo_path"], sha, tmp)
    codes = run_setup(setup, tmp, log)
    if any(c != 0 for c in codes):
        log.append("setup failed, infra\n")
        return 4, log, tmp
    if patch:
        code, _ = run(f"git apply --binary --whitespace=nowarn {patch}", tmp, log=log)
        if code != 0:
            log.append("patch did not apply\n")
            return 3, log, tmp
    env = dict(os.environ, EVAL_REPO=str(tmp), EVAL_BASE_SHA=setup["base_sha"], EVAL_TASK_DIR=str(task.resolve()))
    test = (task / "hidden-test" / "test.sh").resolve()
    code, _ = run(f"bash '{test}'", tmp, env, log)
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return code, log, tmp


def cmd_hidden(a):
    task = Path(a.task_dir)
    setup = json.loads((task / "setup.json").read_text())
    pr = json.loads((task / "pr.json").read_text())
    pr["repo_path"] = source_repo(task, pr, setup, a.repo_path)
    test = task / "hidden-test" / "test.sh"
    if not test.exists():
        sys.exit(f"{test} does not exist")
    if not (setup.get("setup_proof") or {}).get("ok"):
        sys.exit("run  prove.py setup  first")
    proof_dir = task / "hidden-test" / "proof"
    proof_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    runs = [("base", setup["base_sha"], None), ("merge", setup["merge_sha"], None)]
    wrong = task / "hidden-test" / "wrong-fix.patch"
    if wrong.exists():
        runs.append(("wrong-fix", setup["base_sha"], wrong.resolve()))
    for label, sha, patch in runs:
        print(f"running hidden test on {label} ({sha[:12]})")
        code, log, tmp = run_hidden(task, setup, pr, label, sha, patch, a.keep)
        (proof_dir / f"{label}.txt").write_text(portable("".join(log), task, tmp))
        results[label] = {"sha": sha, "exit": code}
        print(f"  exit {code}" + (f", checkout kept at {tmp}" if a.keep else ""))

    verdict = []
    b, m = results["base"]["exit"], results["merge"]["exit"]
    if b == 0:
        verdict.append("REFUSED, the test passes on the unchanged base. A test that passes without the fix is not a test.")
    elif b != 1:
        verdict.append(f"REFUSED, base exited {b}, that is an infrastructure problem not a behaviour failure.")
    if m != 0:
        verdict.append(f"REFUSED, merge exited {m}, the test must pass on the merged PR.")
    if "wrong-fix" in results and results["wrong-fix"]["exit"] != 1:
        verdict.append(f"REFUSED, wrong-fix probe exited {results['wrong-fix']['exit']}, it must fail with 1.")
    ok = not verdict
    (task / "hidden-test" / "proof.json").write_text(json.dumps({
        "ok": ok, "results": results, "verdict": verdict,
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "host": platform.node(),
    }, indent=2))
    print("\n".join(verdict) if verdict else "hidden test proven, fails on base, passes on merge" + (", wrong fix rejected" if "wrong-fix" in results else ""))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("setup", cmd_setup), ("hidden-test", cmd_hidden)):
        s = sub.add_parser(name)
        s.add_argument("--task-dir", required=True)
        s.add_argument("--keep", action="store_true", help="keep the temp checkouts for inspection")
        s.add_argument("--repo-path", help="clone holding base and merge, default the one in build-machine.json, else a fetch from repo_url")
        s.set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
