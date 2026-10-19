"""PreToolUse hook: end the turn when the user asked to stop.

Reads ``PreToolUse`` only (decision D6; a ``PostToolUse`` event is ignored).
Exits 0 with no output on any error, malformed stdin or unknown event, so a
broken plugin looks like no plugin. The first statement takes the start
stamp so any logged ``hook_ms`` covers the ``qsc`` imports as well.
"""
import time; _T0 = time.monotonic()  # first statement: hook_ms counts the qsc imports too
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    try:
        from qsc import hookio, interrupt

        event = hookio.read_event("PreToolUse", started=_T0)
        if event is None:
            return
        hookio.emit(interrupt.handle(event))
    except:  # noqa: E722 - a hook must never fail loudly
        pass


if __name__ == "__main__":
    main()
