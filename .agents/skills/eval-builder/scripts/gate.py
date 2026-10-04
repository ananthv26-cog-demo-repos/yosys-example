#!/usr/bin/env python3
"""Record the engineer's yes at one of the three gates, suitability, proof, freeze.

Usage
  gate.py --task-dir DIR --gate suitability --by "Name" --said "yes, build it" [--said-by WHO] [--recorded-by WHO]

Each gate records three names kept apart. --by is the engineer who approved. --said-by is who
wrote the sentence in --said, the engineer when they typed it (the default), the agent when it
paraphrased, summarised or wrote the sentence from an answer given somewhere else. --recorded-by
is who ran this command, the agent by default, the engineer when they run it by hand. Agent
written words are never recorded as the engineer's.

The record also carries the time and the sha256 of the files the yes covered (suitability,
pr.json and suitability.md. proof, setup.json, setup-proof.txt and everything under
hidden-test/. freeze, prompt.md, criteria.md, isolation.md and the excerpt or source record).
freeze.py refuses a task whose gates.json lacks any of the three or whose covered files changed
after the yes, unless it is run with --one-shot and a reason, which it records in approval.json
for Cognition to see. One message from the engineer is one gate. "Evaluate, build and freeze"
said once at the start approves nothing, ask again at each gate.
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
    ap.add_argument("--by", required=True, help="the engineer who answered at this gate, the approver")
    ap.add_argument("--said", required=True, help="the sentence on record, the engineer's words verbatim when they typed them")
    ap.add_argument("--said-by", help="who wrote the --said sentence, default the approver. The agent's name when it paraphrased or wrote it")
    ap.add_argument("--recorded-by", default="agent", help="who ran this command, default agent. The engineer's name when they run it by hand")
    a = ap.parse_args()
    task = Path(a.task_dir).expanduser().resolve()
    if not a.said.strip() or not a.by.strip():
        sys.exit("gate.py needs the engineer's name and words")
    said_by = (a.said_by or a.by).strip()
    recorded_by = a.recorded_by.strip() or "agent"
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
        "said_by": said_by,
        "recorded_by": recorded_by,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "covers": gate_cover_hashes(task, a.gate),
    }
    p.write_text(json.dumps(data, indent=2) + "\n")
    done = [g for g in GATES if data["gates"].get(g)]
    who = f"approved by {a.by.strip()}, words by {said_by}, recorded by {recorded_by}"
    print(f"recorded the {a.gate} gate over {len(data['gates'][a.gate]['covers'])} files ({', '.join(GATE_COVERS[a.gate])}), {who}, "
          f"{len(done)} of {len(GATES)} gates on record ({', '.join(done)})")


if __name__ == "__main__":
    main()
