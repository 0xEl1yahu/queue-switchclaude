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

# ``time.monotonic()`` when the running hook started (M3 Task 5, M4 Task 2):
# each entry script takes the stamp as its first statement and hands it to
# ``read_event``, which stores it on the event as ``_hook_started``;
# ``start_clock`` copies it here so every log line of the hook can carry
# ``hook_ms``. Without a stamp from the script, ``read_event`` uses the
# parse-time clock, so ``hook_ms`` then covers the work but not the imports.
_started: float | None = None


def read_event(expected: str | tuple[str, ...], started: float | None = None) -> dict | None:
    """Parse the hook event from stdin once.

    Returns None when stdin is empty, not JSON, not a dict, or when the
    payload's hook_event_name is not `expected` (a name, or any of a tuple
    of names). The parsed event is stamped with ``_hook_started`` (a
    ``time.monotonic()`` value) so intake and dispatch can log ``hook_ms``:
    ``started`` when the entry script passes the stamp it took as its first
    statement (then ``hook_ms`` covers the ``qsc`` imports too), else the
    clock as of this call.
    """
    if started is None:
        started = time.monotonic()
    try:
        raw = _read_stdin()
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


def _read_stdin() -> str:
    """All of stdin as text.

    Claude Code writes the event as UTF-8. On Windows a piped stdin decodes
    with the ANSI code page, which garbles (or rejects) any non-ASCII prompt,
    so the bytes are decoded as UTF-8 there instead.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if os.name == "nt" and buffer is not None:
        return buffer.read().decode("utf-8")
    return sys.stdin.read()


def _stamp(event: object) -> float | None:
    """The ``_hook_started`` stamp of ``event`` as a float, or None."""
    value = event.get("_hook_started") if isinstance(event, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def start_clock(event: object) -> None:
    """Remember the event's ``_hook_started`` stamp for ``hook_ms``.

    Called at the top of ``intake.handle`` and ``dispatch.handle``. An event
    built by hand (unit tests) has no stamp, so ``hook_ms`` reads 0.
    """
    global _started
    _started = _stamp(event)


def hook_ms(event: dict | None = None) -> int:
    """Milliseconds since the running hook started; the one place it is computed.

    With ``event``, the time since its ``_hook_started`` stamp; without one,
    the time since the stamp ``start_clock`` remembered. A missing, malformed
    or future stamp reads 0, never a negative or absurd number (M4 review
    focus 2: events built by hand, or a script started another way).
    """
    started = _stamp(event) if event is not None else _started
    if started is None:
        return 0
    return max(0, round((time.monotonic() - started) * 1000))


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
