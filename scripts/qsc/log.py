"""Append-only JSONL decision log, one file per session. Stdlib only.

Logging is best effort: a hook must never fail because the log could not
be written, so record() swallows every exception.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import paths


def record(session_id: str, event: str, **fields) -> None:
    """Append one line {"ts", "event", **fields} to the session's log."""
    try:
        row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
        row.update(fields)
        line = json.dumps(row, default=str)
        path = paths.log_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
