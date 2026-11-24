"""Leaves a grandchild in its own session holding stdout and stderr, then hangs."""
import subprocess
import sys
import time

sys.stdin.read()
subprocess.Popen(["sleep", "3"], start_new_session=True)
time.sleep(30)
