"""Stop-hook dispatch: close the running task, hand the next ready one to Claude.

Stdlib only. The handoff goes through ``hookSpecificOutput.additionalContext``
(spec section 4.3): a ``decision: "block"`` would also keep the turn alive but
shows as a hook error in the UI. A Stop-hook continuation keeps the original
``current_prompt_id``, so intake still sees later messages as mid-turn.
"""
from __future__ import annotations

from . import log, store
from .interrupt import reset_after_interrupt


SHADOW_QUEUED = "shadow_queued"
SHADOW_DISPATCHED = "shadow_dispatched"
_SATISFIED = ("done", SHADOW_DISPATCHED)


def _shadow_ready(s: store.Store) -> list[store.Task]:
    """Shadow-queued tasks whose dependencies are done or shadow-dispatched."""
    by_id = {t.id: t for t in s.tasks}
    out = []
    for t in s.tasks:
        if t.status != SHADOW_QUEUED:
            continue
        if all(d in by_id and by_id[d].status in _SATISFIED for d in t.depends_on):
            out.append(t)
    return out


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
            task_id, _dropped = reset_after_interrupt(s)
            log.record(session_id, "interrupt", task=task_id, via=event.get("hook_event_name"))
            return None

        running = s.get(s.running_task) if s.running_task else None
        if running is not None:
            running.status = "done"
        s.state = "idle"
        s.running_task = None

        if shadow:
            # Report what would run, mark it so the next Stop reports the one after.
            would = _shadow_ready(s)
            if would:
                would[0].status = SHADOW_DISPATCHED
            log.record(session_id, "dispatch", next=would[0].id if would else None, shadow=True)
            return None

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
