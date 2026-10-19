"""Interrupt check at the tool boundary. Stdlib only.

Intake sets ``Store.interrupt`` when the user says "stop" mid-turn. The
next tool boundary is the first place the turn can be ended, so this
handler runs on every tool call: when the flag is set it clears it, marks
the running task ``interrupted`` and ends the turn with ``continue: false``.
The Stop hook does not fire after that.

Registered on ``PreToolUse`` only (M3, decision D6): a ``PostToolUse``
boundary is always followed by the next ``PreToolUse`` or by ``Stop``, and
dispatch honours the flag there, so one launch per tool call loses
nothing. ``handle`` still accepts a ``PostToolUse`` event for callers that
send one. An ``AskUserQuestion`` call is skipped without reading the store:
that turn is already waiting on the user.
"""
from __future__ import annotations

from . import log, store

STOP_REASON = "queue-switchclaude: stopped as requested"

# Tools whose call means the turn is already handing control to the user;
# ending it there would only swallow the question. The flag stays set for
# the next real tool call or for dispatch at Stop.
SKIP_TOOLS = frozenset({"AskUserQuestion"})


def reset_after_interrupt(s: store.Store) -> tuple[str | None, list[store.Task]]:
    """Clear the flag and put the store back to idle.

    Returns the interrupted task id and the queued tasks that were dropped.
    An explicit "stop" is the user's Esc, so decision D5 applies: queued and
    ready tasks are dropped rather than left stranded behind an interrupted
    dependency; parked and done items survive. Shared with dispatch, which
    can see the flag when the turn ended without another tool call.
    """
    task_id = s.running_task
    task = s.get(task_id) if task_id else None
    if task is not None:
        task.status = "interrupted"
    dropped = [t for t in s.tasks if t.status in ("queued", "planned")]
    for t in dropped:
        t.status = "dropped"
    s.interrupt = False
    s.state = "idle"
    s.running_task = None
    s.pending_task = None  # a task being planned or awaiting approval is dropped above
    return task_id, dropped


def stop_reason(dropped: list[store.Task]) -> str:
    if not dropped:
        return STOP_REASON
    listed = ", ".join(f"{t.id} ({t.text})" for t in dropped)
    return f"{STOP_REASON}; dropped queued tasks: {listed}"


def handle(event: dict) -> dict | None:
    """Return ``{"continue": False, ...}`` when the interrupt flag is set, else None."""
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if event.get("tool_name") in SKIP_TOOLS:
        return None
    # Cheap unlocked peek first: this runs on every tool call and the flag
    # is almost always clear, so do not take the lock or touch the file.
    if not store.load(session_id).interrupt:
        return None
    with store.locked(session_id) as s:
        if not s.interrupt:
            return None
        task_id, dropped = reset_after_interrupt(s)
    log.record(session_id, "interrupt", task=task_id, dropped=[t.id for t in dropped],
               via=event.get("hook_event_name"))
    return {"continue": False, "stopReason": stop_reason(dropped)}
