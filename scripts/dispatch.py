#!/usr/bin/env python3
"""Stop hook: close the finished task and hand the next queued one to Claude.

Exits 0 with no output on any error, malformed stdin or unknown event, so a
broken plugin looks like no plugin.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    try:
        from qsc import dispatch, hookio

        event = hookio.read_event("Stop")
        if event is None:
            return
        hookio.emit(dispatch.handle(event, shadow=hookio.shadow()))
    except:  # noqa: E722 - a hook must never fail loudly
        pass


if __name__ == "__main__":
    main()
