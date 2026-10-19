"""Hook stdin/stdout helpers. Stdlib only.

Every hook script reads one JSON event from stdin and may write one JSON
object to stdout. Anything unexpected on either side must look like no
plugin: read_event returns None, emit writes nothing, neither raises.
"""
from __future__ import annotations

import json
import os
import sys
import time

_TRUTHY = {"1", "true", "yes"}

# ``time.monotonic()`` when the running hook parsed its event (M3 Task 5):
# ``read_event`` stamps it on the event as ``_hook_started`` and ``start_clock``
# copies it here so every log line of the hook can carry ``hook_ms``.
_started: float | None = None


def read_event(expected: str | tuple[str, ...]) -> dict | None:
    """Parse the hook event from stdin once.

    Returns None when stdin is empty, not JSON, not a dict, or when the
    payload's hook_event_name is not `expected` (a name, or any of a tuple
    of names). The parsed event is stamped with ``_hook_started`` (a
    ``time.monotonic()`` value) so intake and dispatch can log ``hook_ms``.
    """
    started = time.monotonic()
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
    event["_hook_started"] = started
    return event


def start_clock(event: object) -> None:
    """Remember the event's ``_hook_started`` stamp for ``hook_ms``.

    Called at the top of ``intake.handle`` and ``dispatch.handle``. An event
    built by hand (unit tests) has no stamp, so ``hook_ms`` reads 0.
    """
    global _started
    value = event.get("_hook_started") if isinstance(event, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _started = None
    else:
        _started = float(value)


def hook_ms() -> int:
    """Milliseconds since the running hook parsed its event; 0 without a stamp."""
    if _started is None:
        return 0
    return max(0, round((time.monotonic() - _started) * 1000))


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


def shadow_compare() -> bool:
    """True when the plugin's `shadow_compare` user setting is on.

    In shadow mode this makes intake also ask the model about messages the
    rules already decided, so the log holds both answers (M1 Task 10).
    """
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", "")
    return value.strip().lower() in _TRUTHY
