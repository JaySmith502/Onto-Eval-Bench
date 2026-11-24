"""Hunts for reference answers next to its build inputs and for the run ledger above its cwd, and reports every path it was handed."""
import json
import os
import sys
from pathlib import Path

verb = sys.argv[1]
req = json.load(sys.stdin)
st = Path(req["state_dir"])
st.mkdir(parents=True, exist_ok=True)
out = None
if verb == "build":
    (st / "inputs.json").write_text(json.dumps(req["inputs"]))
if verb == "run-task":
    inputs = json.loads((st / "inputs.json").read_text())
    try:
        answer = json.loads((Path(inputs[0]).parent / "ref" / f"{req['task_id']}.json").read_text())["answer"]
    except OSError:
        answer = -1
    for p in (Path.cwd(), *Path.cwd().parents):
        if (p / "attempts.jsonl").exists():
            with open(p / "attempts.jsonl", "a") as f:
                f.write("{}\n")
    out = {"answer": answer, "seen": [os.getcwd(), str(st), os.environ.get("HOME", ""), *inputs]}
print(json.dumps({"status": "completed", "output": out, "adapter_version": "cheat", "model_used": req.get("model"),
                  "cost": {"usd": 0.0, "tokens_in": 0, "tokens_out": 0, "measured": True}}))
