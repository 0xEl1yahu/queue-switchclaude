#!/usr/bin/env python3
"""UserPromptSubmit hook entry point: stdin event -> qsc.intake.handle -> stdout.

Any failure, malformed stdin or foreign event exits 0 with no output, so a
broken plugin looks like no plugin.
"""
try:
    from qsc import hookio, intake

    event = hookio.read_event("UserPromptSubmit")
    if event is not None:
        hookio.emit(intake.handle(event, shadow=hookio.shadow()))
except BaseException:
    pass
