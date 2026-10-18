"""Where the plugin keeps per-session state. Stdlib only."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """$CLAUDE_PLUGIN_DATA when set, else ~/.claude/queue-switchclaude."""
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(env) if env else Path.home() / ".claude" / "queue-switchclaude"


def store_path(session_id: str) -> Path:
    return data_dir() / "sessions" / f"{session_id}.json"


def log_path(session_id: str) -> Path:
    return data_dir() / "log" / f"{session_id}.jsonl"
