"""Reports what it can reach: reads settings.read, opens a TCP socket to settings.connect, lists its env and state access."""
import json
import os
import socket
import sys

req = json.loads(sys.stdin.read())
s = req.get("settings", {})
out = {}
if "read" in s:
    try:
        out["read"] = open(s["read"]).read()
    except OSError as e:
        out["read_error"] = type(e).__name__
if "connect" in s:
    host, port = s["connect"].split(":")
    try:
        socket.create_connection((host, int(port)), timeout=3).close()
        out["connect"] = "ok"
    except OSError as e:
        out["connect_error"] = type(e).__name__
out["state_write"] = os.access(os.environ["OBS_STATE_DIR"], os.W_OK)
out["env"] = sorted(os.environ)
print(json.dumps({"status": "completed", "output": out}))
