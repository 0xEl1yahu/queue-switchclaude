"""Fake `claude` CLI for tests on Windows, run through claude.cmd.

Same contract as the sh fake beside it: sleeps when FAKE_CLAUDE_SLEEP is
set, otherwise prints FAKE_CLAUDE_OUTPUT and exits 0.
"""
import os
import sys
import time

if os.environ.get("FAKE_CLAUDE_SLEEP"):
    time.sleep(float(os.environ["FAKE_CLAUDE_SLEEP"]))
    sys.exit(0)
sys.stdout.write(os.environ.get("FAKE_CLAUDE_OUTPUT", "") + "\n")
