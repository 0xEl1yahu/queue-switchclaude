"""Hook stdin/stdout helpers. Stdlib only.

Every hook script reads one JSON event from stdin and may write one JSON
object to stdout. Anything unexpected on either side must look like no
plugin: read_event returns None, emit writes nothing, neither raises.
"""
from __future__ import annotations

import json
import os
import sys

_TRUTHY = {"1", "true", "yes"}


def read_event(expected: str | tuple[str, ...]) -> dict | None:
    """Parse the hook event from stdin once.

    Returns None when stdin is empty, not JSON, not a dict, or when the
    payload's hook_event_name is not `expected` (a name, or any of a tuple
    of names).
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        event = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    name = event.get("hook_event_name")
    if isinstance(expected, str):
        accepted = (expected,)
    else:
        accepted = tuple(expected)
    if name not in accepted:
        return None
    return event


def emit(obj: dict | None) -> None:
    """Write `obj` as JSON to stdout; write nothing for None. Never raises."""
    if obj is None:
        return
    try:
        text = json.dumps(obj)
    except Exception:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def shadow() -> bool:
    """True when the plugin's `shadow` user setting is on."""
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_SHADOW", "")
    return value.strip().lower() in _TRUTHY
