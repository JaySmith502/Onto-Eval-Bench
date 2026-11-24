"""The first time it sees a task it answers wrong and reports runtime_failure; afterwards it answers right."""
import json
import os
import sys

verb = sys.argv[1]
req = json.load(sys.stdin)
os.makedirs(req["state_dir"], exist_ok=True)
status, out = "completed", None
if verb == "run-task":
    flag = os.path.join(req["state_dir"], "seen-" + req["task_id"])
    if os.path.exists(flag):
        out = {"answer": req["input"]["x"] * 2}
    else:
        open(flag, "w").close()
        status, out = "runtime_failure", {"answer": req["input"]["x"] * 2 + 1}
print(json.dumps({"status": status, "output": out, "adapter_version": "reroll", "model_used": req.get("model"),
                  "cost": {"usd": 0.0, "tokens_in": 0, "tokens_out": 0, "measured": True}}))
