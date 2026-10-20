"""Interrupt check at the tool boundary. Stdlib only.

Intake sets ``Store.interrupt`` when the user says "stop" mid-turn. The
next tool boundary is the first place the turn can be ended, so this
handler runs on every tool call: when the flag is set it resets the turn
(``reset_turn``, the same reset intake applies after Esc) and ends it with
``continue: false``. The Stop hook does not fire after that.

Registered on ``PreToolUse`` only (M3, decision D6): a ``PostToolUse``
boundary is always followed by the next ``PreToolUse`` or by ``Stop``, and
dispatch honours the flag there, so one launch per tool call loses
nothing. Since M4 ``handle`` reads ``PreToolUse`` only as well: any other
event name (``PostToolUse`` included) returns None without touching the
store. An ``AskUserQuestion`` call is skipped without reading the store:
that turn is already waiting on the user.
"""
from __future__ import annotations

import contextlib
from typing import NamedTuple

from . import hookio, log, paths, store

SHADOW_QUEUED = "shadow_queued"
STOP_REASON = "queue-switchclaude: stopped as requested"
HOOK_EVENT = "PreToolUse"

# Tools whose call means the turn is already handing control to the user;
# ending it there would only swallow the question. The flag stays set for
# the next real tool call or for dispatch at Stop.
SKIP_TOOLS = frozenset({"AskUserQuestion"})


class Reset(NamedTuple):
    """What ``reset_turn`` did: the task it interrupted, the tasks it
    dropped, the task it put back in the queue, and the tasks it rewired
    past the ones it interrupted or dropped."""

    interrupted: str | None
    dropped: list[store.Task]
    requeued: str | None
    rewired: list[store.Task]


def reset_turn(s: store.Store) -> Reset:
    """Cut the current turn short and leave the store idle.

    The one reset for both ways a user ends a turn: "stop" (the interrupt
    flag, honoured here at the next tool call or by dispatch at Stop) and
    Esc (intake sees a new ``prompt_id`` while the store still says
    ``running`` or ``planning``). By state:

    * ``running``: the running task becomes ``interrupted`` (a paused one
      stays paused) and every ``queued`` and ``shadow_queued`` task is
      ``dropped`` rather than left behind the interrupted work (D5).
    * ``planning``: the user cut planning, not work, so nothing is dropped;
      the task being planned goes back to ``queued`` with its half-written
      plan file deleted, and dispatch plans it afresh later. A pending task
      the user cancelled, parked or dropped meanwhile is left as it is.

    Either way a ``planned`` task (a plan waiting for approval that an
    unrelated turn deferred) keeps its status and its plan, and dispatch
    presents it again; ``parked``, ``paused``, ``failed_verify`` and ``done``
    tasks are untouched. Tasks that depended on the interrupted or dropped
    ones are rewired past them (``Store.rewire``). The flag is cleared and
    the running and pending slots are emptied.
    """
    interrupted = None
    requeued = None
    dropped: list[store.Task] = []
    if s.state == "planning":
        task = s.get(s.pending_task) if s.pending_task else None
        if task is not None and task.status == "queued":
            task.attempts = 0
            with contextlib.suppress(OSError):
                paths.plan_path(s.session_id, task.id).unlink()
            requeued = task.id
    elif s.state == "running" or s.running_task:
        task = s.get(s.running_task) if s.running_task else None
        if task is not None and task.status == "running":
            task.status = "interrupted"
            interrupted = task.id
        dropped = [t for t in s.tasks if t.status in ("queued", SHADOW_QUEUED)]
        for t in dropped:
            t.status = "dropped"
    rewired = s.rewire()
    s.interrupt = False
    s.state = "idle"
    s.running_task = None
    s.pending_task = None
    return Reset(interrupted, dropped, requeued, rewired)


def stop_reason(reset: Reset) -> str:
    parts = [STOP_REASON]
    if reset.dropped:
        parts.append("dropped queued tasks: " + ", ".join(f"{t.id} ({t.text})" for t in reset.dropped))
    if reset.requeued:
        parts.append(f"{reset.requeued} is queued again")
    return "; ".join(parts)


def handle(event: dict) -> dict | None:
    """Return ``{"continue": False, ...}`` when the interrupt flag is set, else None.

    Only a ``PreToolUse`` event is acted on; anything else returns None.
    """
    if event.get("hook_event_name") != HOOK_EVENT:
        return None
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
        reset = reset_turn(s)
    log.record(session_id, "interrupt", hook_ms=hookio.hook_ms(event), task=reset.interrupted, dropped=[t.id for t in reset.dropped],
               requeued=reset.requeued, rewired=[t.id for t in reset.rewired],
               via=event.get("hook_event_name"))
    return {"continue": False, "stopReason": stop_reason(reset)}
