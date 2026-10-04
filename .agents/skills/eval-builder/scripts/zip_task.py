#!/usr/bin/env python3
"""Zip a frozen task folder for the laptop that will run it. Refuses the build only files, build-machine.json and
candidates.json, which name folders and sessions on the build machine and have no place on another one.

Usage
  zip_task.py --task-dir DIR [--out FILE]        default FILE is <task>.zip next to the folder

The zip unpacks to <task>/... so  unzip <task>.zip -d ~/evals/  puts it where evals check expects it.
"""
import argparse
import sys
import zipfile
from pathlib import Path

BUILD_ONLY = ("build-machine.json", "candidates.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    task = Path(a.task_dir).resolve()
    if not (task / "approval.json").is_file():
        sys.exit(f"{task.name} is not frozen, no approval.json, run freeze.py first")
    present = [n for n in BUILD_ONLY if (task / n).exists()]
    if present:
        sys.exit(f"{task.name} still holds {', '.join(present)}, build only files that name this machine's folders and sessions, "
                 f"delete them first (rm {' '.join(str(task / n) for n in present)}) and zip again")
    if (task / "runs").exists():
        sys.exit(f"{task.name} holds a runs folder, attempts belong to the laptop that ran them, this zip is the frozen task only")
    out = Path(a.out) if a.out else task.with_suffix(".zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(task.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, f"{task.name}/{p.relative_to(task)}")
    print(f"wrote {out}, unzip it with  unzip {out.name} -d ~/evals/  on the laptop that runs the attempts")


if __name__ == "__main__":
    main()
