"""Where the plugin keeps per-session state. Stdlib only."""
from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def safe_id(session_id: str) -> str:
    """A session id as a file-name stem: anything outside [A-Za-z0-9_-] becomes '_'.

    Claude Code sends UUIDs, so this only matters for a crafted event; it
    keeps '../x' from escaping the sessions/ and log/ directories.
    """
    cleaned = _SAFE.sub("_", str(session_id))
    return cleaned or "_"


def data_dir() -> Path:
    """$CLAUDE_PLUGIN_DATA when set, else ~/.claude/queue-switchclaude."""
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(env) if env else Path.home() / ".claude" / "queue-switchclaude"


def store_path(session_id: str) -> Path:
    return data_dir() / "sessions" / f"{safe_id(session_id)}.json"


def log_path(session_id: str) -> Path:
    return data_dir() / "log" / f"{safe_id(session_id)}.jsonl"


def pointer_path() -> Path:
    """The file holding the id of the session intake saw last.

    Intake rewrites it on every event, best effort, so ``scripts/queue_cli.py``
    (the Bash side of ``/queue``) can find the store without being told the
    session id. One pointer per data dir: the most recent session wins.
    """
    return data_dir() / "last_session"
