#!/usr/bin/env python3
"""PreToolUse / PostToolUse hook: end the turn when the user asked to stop.

Exits 0 with no output on any error, malformed stdin or unknown event, so a
broken plugin looks like no plugin.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    try:
        from qsc import hookio, interrupt

        event = hookio.read_event(("PreToolUse", "PostToolUse"))
        if event is None:
            return
        hookio.emit(interrupt.handle(event))
    except:  # noqa: E722 - a hook must never fail loudly
        pass


if __name__ == "__main__":
    main()
