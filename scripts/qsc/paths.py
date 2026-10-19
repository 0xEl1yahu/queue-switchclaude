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


PLUGIN_DATA_ROOT = Path.home() / ".claude" / "plugins" / "data"
PLUGIN_NAME = "queue-switchclaude"


def _installed_data_dir() -> Path | None:
    """The data dir Claude Code gave this plugin, found without the env var.

    Hooks receive ``CLAUDE_PLUGIN_DATA``; a command Claude runs through the
    Bash tool does not (``queue_cli.py``). Claude Code keeps plugin data at
    ``~/.claude/plugins/data/<plugin id>/`` where the id starts with the
    plugin name (``queue-switchclaude-inline`` for ``--plugin-dir`` loads).
    When several exist, the one whose pointer file was written last wins.
    """
    try:
        candidates = [
            d for d in PLUGIN_DATA_ROOT.iterdir()
            if d.is_dir() and d.name.startswith(PLUGIN_NAME)
        ]
    except OSError:
        return None
    if not candidates:
        return None

    def recency(d: Path) -> float:
        try:
            return (d / "last_session").stat().st_mtime
        except OSError:
            return 0.0

    return max(candidates, key=recency)


def data_dir() -> Path:
    """$CLAUDE_PLUGIN_DATA when set, else the installed plugin data dir, else
    ~/.claude/queue-switchclaude."""
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    installed = _installed_data_dir()
    if installed is not None:
        return installed
    return Path.home() / ".claude" / PLUGIN_NAME


def store_path(session_id: str) -> Path:
    return data_dir() / "sessions" / f"{safe_id(session_id)}.json"


def log_path(session_id: str) -> Path:
    return data_dir() / "log" / f"{safe_id(session_id)}.jsonl"


def session_dir(session_id: str) -> Path:
    """The per-session directory holding plans and handoffs: ``sessions/<safe_id>/``.

    It sits next to the session's ``<safe_id>.json`` store. Plans and handoffs
    live here, under the plugin data dir, rather than in the user's repository
    as spec §4.3 (``plans/pN.md``) suggested, so the working tree stays clean.
    """
    return data_dir() / "sessions" / safe_id(session_id)


def plan_path(session_id: str, task_id: str) -> Path:
    """``sessions/<sid>/plans/<task_id>.md``: the plan Claude writes for a task."""
    return session_dir(session_id) / "plans" / f"{safe_id(task_id)}.md"


def handoff_path(session_id: str, task_id: str) -> Path:
    """``sessions/<sid>/handoffs/<task_id>.md``: what dispatch files when a task closes."""
    return session_dir(session_id) / "handoffs" / f"{safe_id(task_id)}.md"


def pointer_path() -> Path:
    """The file holding the id of the session intake saw last.

    Intake rewrites it on every event, best effort, so ``scripts/queue_cli.py``
    (the Bash side of ``/queue``) can find the store without being told the
    session id. One pointer per data dir: the most recent session wins.
    """
    return data_dir() / "last_session"
