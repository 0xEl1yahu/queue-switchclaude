"""Interrupt check for PreToolUse / PostToolUse. Stdlib only.

Intake sets ``Store.interrupt`` when the user says "stop" mid-turn. The
next tool boundary is the first place the turn can be ended, so this
handler runs on every tool call: when the flag is set it clears it, marks
the running task ``interrupted`` and ends the turn with ``continue: false``.
The Stop hook does not fire after that.
"""
from __future__ import annotations

from . import log, store

STOP_REASON = "queue-switchclaude: stopped as requested"


def reset_after_interrupt(s: store.Store) -> str | None:
    """Clear the flag and put the store back to idle; returns the interrupted task id.

    Shared with dispatch, which can see the flag when the turn ended without
    another tool call (a text-only tail has no tool boundary).
    """
    task_id = s.running_task
    task = s.get(task_id) if task_id else None
    if task is not None:
        task.status = "interrupted"
    s.interrupt = False
    s.state = "idle"
    s.running_task = None
    return task_id


def handle(event: dict) -> dict | None:
    """Return ``{"continue": False, ...}`` when the interrupt flag is set, else None."""
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    # Cheap unlocked peek first: this runs on every tool call and the flag
    # is almost always clear, so do not take the lock or touch the file.
    if not store.load(session_id).interrupt:
        return None
    with store.locked(session_id) as s:
        if not s.interrupt:
            return None
        task_id = reset_after_interrupt(s)
    log.record(session_id, "interrupt", task=task_id, via=event.get("hook_event_name"))
    return {"continue": False, "stopReason": STOP_REASON}
