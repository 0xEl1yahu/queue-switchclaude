"""UserPromptSubmit hook entry point: stdin event -> qsc.intake.handle -> stdout.

Any failure, malformed stdin or foreign event exits 0 with no output, so a
broken plugin looks like no plugin. The first statement takes the start
stamp so the logged ``hook_ms`` covers the ``qsc`` imports as well.
"""
import time; _T0 = time.monotonic()  # first statement: hook_ms counts the qsc imports too
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from qsc import hookio, intake

    event = hookio.read_event("UserPromptSubmit", started=_T0)
    if event is not None:
        hookio.emit(intake.handle(event, shadow=hookio.shadow()))
except BaseException:
    pass
