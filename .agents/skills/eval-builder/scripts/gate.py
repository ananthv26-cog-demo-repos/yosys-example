#!/usr/bin/env python3
"""Record the engineer's yes at one of the three gates, suitability, proof, freeze.

Usage
  gate.py --task-dir DIR --gate suitability --by "Name" --said "yes, build it"

Each gate is the engineer's own words, typed or pasted, with the time it was recorded and
the sha256 of the files that yes covered (suitability, pr.json and suitability.md. proof,
setup.json, setup-proof.txt and everything under hidden-test/. freeze, prompt.md,
criteria.md, isolation.md and the excerpt or source record). freeze.py refuses a task whose
gates.json lacks any of the three or whose covered files changed after the yes, unless it
is run with --one-shot and a reason, which it records in approval.json for Cognition to see.
One message from the engineer is one gate. "Evaluate, build and freeze" said once at the
start approves nothing, ask again at each gate.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import GATE_COVERS, GATES, gate_cover_hashes  # noqa: E402

# A yes needs something to cover. These must exist before the gate is asked.
GATE_NEEDS = {
    "suitability": ("suitability.md",),
    "proof": ("hidden-test/test.sh", "hidden-test/proof.json", "setup.json"),
    "freeze": ("prompt.md", "criteria.md", "isolation.md"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--gate", required=True, choices=GATES)
    ap.add_argument("--by", required=True, help="the engineer who said yes")
    ap.add_argument("--said", required=True, help="their words, verbatim")
    a = ap.parse_args()
    task = Path(a.task_dir).expanduser().resolve()
    if not a.said.strip() or not a.by.strip():
        sys.exit("gate.py needs the engineer's name and words")
    missing = [rel for rel in GATE_NEEDS[a.gate] if not (task / rel).is_file()]
    if missing:
        sys.exit(f"the {a.gate} gate covers {', '.join(missing)}, not written yet, finish that step before asking")
    p = task / "gates.json"
    data = json.loads(p.read_text()) if p.is_file() else {"gates": {}}
    data.setdefault("gates", {})
    if data["gates"].get(a.gate):
        print(f"{a.gate} gate already recorded at {data['gates'][a.gate]['at']}, replacing it")
    data["gates"][a.gate] = {
        "approved_by": a.by.strip(),
        "said": a.said.strip(),
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "covers": gate_cover_hashes(task, a.gate),
    }
    p.write_text(json.dumps(data, indent=2) + "\n")
    done = [g for g in GATES if data["gates"].get(g)]
    print(f"recorded the {a.gate} gate over {len(data['gates'][a.gate]['covers'])} files ({', '.join(GATE_COVERS[a.gate])}), "
          f"{len(done)} of {len(GATES)} gates on record ({', '.join(done)})")


if __name__ == "__main__":
    main()
