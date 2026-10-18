"""Stop-hook dispatch: close the running task, hand the next ready one to Claude.

Stdlib only. The handoff goes through ``hookSpecificOutput.additionalContext``
(spec section 4.3): a ``decision: "block"`` would also keep the turn alive but
shows as a hook error in the UI. A Stop-hook continuation keeps the original
``current_prompt_id``, so intake still sees later messages as mid-turn.
"""
from __future__ import annotations

from . import log, store
from .interrupt import reset_after_interrupt


def _handoff(task: store.Task) -> dict:
    text = (
        f"queue-switchclaude: the user queued task {task.id} to run after the "
        f"previous work. Do it now: {task.text}"
    )
    return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}}


def handle(event: dict, shadow: bool = False) -> dict | None:
    """Mark the running task done and dispatch the next ready task, if any."""
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    with store.locked(session_id) as s:
        if s.interrupt:
            # The user asked to stop but no tool call came after the flag was
            # set; the turn is ending now, so apply the same reset and dispatch
            # nothing.
            task_id = reset_after_interrupt(s)
            log.record(session_id, "interrupt", task=task_id, via=event.get("hook_event_name"))
            return None

        running = s.get(s.running_task) if s.running_task else None
        if running is not None:
            running.status = "done"
        s.state = "idle"
        s.running_task = None

        ready = s.ready()
        if not ready:
            log.record(session_id, "dispatch", next=None, shadow=shadow)
            return None
        nxt = ready[0]

        # Spin guard: a Stop fired by our own continuation must not push the
        # same task twice.
        if event.get("stop_hook_active") and s.last_dispatched == nxt.id:
            log.record(session_id, "dispatch", next=nxt.id, shadow=shadow, spin_guard=True)
            return None

        log.record(session_id, "dispatch", next=nxt.id, shadow=shadow)
        if shadow:
            return None

        nxt.status = "running"
        s.running_task = nxt.id
        s.state = "running"
        s.last_dispatched = nxt.id
        return _handoff(nxt)
