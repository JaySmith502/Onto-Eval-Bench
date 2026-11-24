"""Completes with settings["cost"] spliced in verbatim as the cost (no cost key when absent)."""
import json
import sys

req = json.load(sys.stdin)
raw = req.get("settings", {}).get("cost")
cost = "" if raw is None else f', "cost": {raw}'
print('{"status": "completed", "output": {"answer": 1}, "adapter_version": "badcost", "model_used": %s%s}'
      % (json.dumps(req.get("model")), cost))
