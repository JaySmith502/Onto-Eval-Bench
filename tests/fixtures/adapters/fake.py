"""Fake adapter driven by its settings: wrong_tasks, sleep_tasks (+sleep_s), fail_tasks, unsupported_tasks (csv task ids)."""
import json
import sys
import time

verb = sys.argv[1]
req = json.load(sys.stdin)
s = req.get("settings", {})
csv = lambda k: set(filter(None, s.get(k, "").split(",")))


def reply(status="completed", output=None):
    print(json.dumps({"status": status, "output": output, "adapter_version": "fake-1",
                      "cost": {"usd": 0.01, "tokens_in": 10, "tokens_out": 5, "measured": True}}))


if verb == "run-task":
    t = req["task_id"]
    if t in csv("sleep_tasks"):
        time.sleep(float(s.get("sleep_s", "5")))
    if t in csv("fail_tasks"):
        reply("runtime_failure")
    elif t in csv("unsupported_tasks"):
        reply("unsupported")
    else:
        reply(output={"answer": req["input"]["x"] * 2 + (1 if t in csv("wrong_tasks") else 0)})
else:
    reply()
