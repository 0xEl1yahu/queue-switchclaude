"""The ``queue:`` control channel. Stdlib only.

``run(s, command, session_id=None)`` executes one user command against an
already-locked store and returns the text intake shows the user. The rule
table turns a ``queue: ...`` or ``/queue ...`` prompt into the bare command
(``list``, ``cancel p2``, ...); an empty command means ``list``. Control
never calls the classifier and never counts as a turn, so every reply is
short enough to sit above the input box: one line, always.

Commands (first word, case-insensitive):

    list          the running, queued, parked, paused, planned and
                  failed-verify tasks, on ONE line (the mid-run notification
                  shows a single line); the planned task waiting for the
                  user's approval reads ``planned (awaiting approval)``
    show pN       ``pN <status>: <text>`` plus ``; plan: <path>`` and
                  ``; handoff: <path>`` when those files exist for the
                  session, ``; verify: <cmd>`` when set, ``; auto`` when set
    cancel pN     a queued, parked, paused, planned or failed-verify task
                  becomes ``cancelled``; a task that waited on it now waits
                  on what it waited on, and the reply says so
    promote pN    a parked, paused or failed-verify task becomes ``queued``
    park pN       a queued task becomes ``parked``
    drop          every queued and parked task becomes ``dropped``; the
                  tasks that waited on them are rewired the same way
    pause         ``Store.paused = True``: dispatch runs nothing
    resume        ``Store.paused = False``
    resume pN     a paused task becomes ``queued`` (same as promote)
    verify pN <command...>
                  the rest of the line, verbatim, becomes the task's verify
                  command: on Stop it decides done (exit 0) instead of the
                  judge (decision D4); live or planned tasks only
    auto pN       the task skips the approval gate (decision D3): its plan
                  flows straight into execution
    manual pN     the task waits for approval again (the default)
    approve       the plan waiting for approval runs now: the pending task
                  becomes ``running`` and so does the store
    help          this list; so does any unknown word

In shadow mode intake records tasks as ``shadow_queued``; ``cancel``,
``park`` and ``drop`` act on those too, so the control channel can edit what
shadow mode recorded. A failure reply names every status the command
accepts: ``pN is not queued, parked, paused, planned or failed_verify``,
``pN is not parked, paused or failed_verify``.

``session_id`` is only needed by ``show``, which resolves the plan and
handoff paths through ``paths``; without it ``show`` omits them.
"""
from __future__ import annotations

from . import paths, store

TEXT_WIDTH = 60
ITEM_WIDTH = 36
COMMANDS = (
    "list", "show", "cancel", "promote", "park", "drop", "pause", "resume",
    "verify", "auto", "manual", "approve", "help",
)

HELP = (
    "commands: list | show pN | cancel pN | promote pN (parked, paused or failed verify) | "
    "park pN | drop | pause | resume [pN] | verify pN <command> | auto pN | "
    "manual pN | approve | help"
)

# The status word `list` shows for the planned task the session is waiting on.
AWAITING_APPROVAL = "planned (awaiting approval)"

# What intake records in shadow mode (same literal as intake and dispatch).
SHADOW_QUEUED = "shadow_queued"


def _clip(text: str) -> str:
    """One line of at most TEXT_WIDTH characters, with an ellipsis when cut."""
    flat = " ".join(text.split())
    if len(flat) <= TEXT_WIDTH:
        return flat
    return flat[:TEXT_WIDTH] + "…"


def _status_word(s: store.Store, t: store.Task) -> str:
    """``planned (awaiting approval)`` for the task whose plan is on screen."""
    if t.status == "planned" and s.state == "awaiting_approval" and s.pending_task == t.id:
        return AWAITING_APPROVAL
    return t.status


def _item(s: store.Store, t: store.Task) -> str:
    """``p2 queued after p1: add a goodbye.py…`` with the text cut to ITEM_WIDTH."""
    after = f" after {', '.join(t.depends_on)}" if t.depends_on else ""
    flat = " ".join(t.text.split())
    if len(flat) > ITEM_WIDTH:
        flat = flat[:ITEM_WIDTH] + "…"
    return f"{t.id} {_status_word(s, t)}{after}: {flat}"


def _list(s: store.Store) -> str:
    """One line: the notification area shows a single line of a systemMessage."""
    live = s.candidates()
    body = "; ".join(_item(s, t) for t in live) if live else "queue is empty"
    if s.paused:
        body += "; queue is paused, send 'queue: resume' to dispatch again"
    return body


def _show(s: store.Store, task_id: str, session_id: str | None) -> str:
    """One line about one task, with the files that exist for it."""
    t = s.get(task_id)
    if t is None:
        return f"no task {task_id}"
    parts = [f"{t.id} {t.status}: {_clip(t.text)}"]
    if session_id:
        plan = paths.plan_path(session_id, t.id)
        if plan.is_file():
            parts.append(f"plan: {plan}")
        handoff = paths.handoff_path(session_id, t.id)
        if handoff.is_file():
            parts.append(f"handoff: {handoff}")
    if t.verify:
        parts.append(f"verify: {' '.join(t.verify.split())}")
    if t.auto:
        parts.append("auto")
    return "; ".join(parts)


def _task_id(arg: str) -> str:
    """Accept ``p2``, ``P2`` or a bare ``2``."""
    arg = arg.strip().lower()
    return f"p{arg}" if arg.isdigit() else arg


def _live(s: store.Store, task_id: str) -> "store.Task | str":
    """The live task (queued, parked, paused, running, planned, failed_verify)
    or the reply explaining why there is none."""
    t = s.get(task_id)
    if t is None:
        return f"no task {task_id}"
    if t.status not in store.CANDIDATE_STATUSES:
        return f"{task_id} is {t.status}, not live"
    return t


def rewired_note(tasks: list[store.Task]) -> str:
    """``; p3 now after p1`` for each task ``Store.rewire`` moved, or ``""``."""
    return "".join(
        f"; {t.id} now after {', '.join(t.depends_on) if t.depends_on else 'nothing'}"
        for t in tasks
    )


def _cancel(s: store.Store, task_id: str) -> str:
    t = s.get(task_id)
    if t is None:
        return f"no task {task_id}"
    waiting = s.dependants([task_id])
    if t.status == SHADOW_QUEUED:
        t.status = "cancelled"
        s.rewire()
        return f"cancelled {t.id}: {t.text}{rewired_note(waiting)}"
    t = s.cancel(task_id)
    if t is None:
        return f"{task_id} is not queued, parked, paused, planned or failed_verify"
    return f"cancelled {t.id}: {t.text}{rewired_note(waiting)}"


def _promote(s: store.Store, task_id: str) -> str:
    if s.get(task_id) is None:
        return f"no task {task_id}"
    t = s.promote(task_id)
    if t is None:
        return f"{task_id} is not parked, paused or failed_verify"
    return f"promoted {t.id} to the queue"


def _park(s: store.Store, task_id: str) -> str:
    t = s.get(task_id)
    if t is None:
        return f"no task {task_id}"
    if t.status not in ("queued", SHADOW_QUEUED):
        return f"{task_id} is not queued"
    t.status = "parked"
    return f"parked {t.id}"


def _drop(s: store.Store) -> str:
    dropped = [t for t in s.tasks if t.status in ("queued", "parked", SHADOW_QUEUED)]
    waiting = s.dependants([t.id for t in dropped])
    for t in dropped:
        t.status = "dropped"
    s.rewire()
    n = len(dropped)
    noun = "task" if n == 1 else "tasks"
    if not dropped:
        return f"dropped 0 {noun}"
    return f"dropped {n} {noun}: " + ", ".join(t.id for t in dropped) + rewired_note(waiting)


def _verify(s: store.Store, task_id: str, command: str) -> str:
    """Set the task's verify command to ``command`` (the rest of the line)."""
    t = _live(s, task_id)
    if isinstance(t, str):
        return t
    t.verify = command
    return f"{t.id} verifies with: {command}"


def _auto(s: store.Store, task_id: str, auto: bool) -> str:
    t = _live(s, task_id)
    if isinstance(t, str):
        return t
    t.auto = auto
    if auto:
        return f"{t.id} will run without approval"
    return f"{t.id} will wait for approval"


def _approve(s: store.Store, task_id: str | None) -> str:
    """Run the plan that is waiting: the same transition as typing "lgtm".

    The pending task becomes ``running`` and so does the store; dispatch and
    intake hand Claude the execution text (Task 5 ``dispatch.start``).
    """
    pending = s.get(s.pending_task) if s.pending_task else None
    waiting = (
        s.state == "awaiting_approval"
        and pending is not None
        and pending.status == "planned"
    )
    if not waiting:
        return "nothing is waiting for approval"
    if task_id is not None and task_id != pending.id:
        return f"{task_id} is not waiting for approval; {pending.id} is"
    pending.status = "running"
    s.running_task = pending.id
    s.state = "running"
    s.pending_task = None
    return f"approved {pending.id}, it runs now"


def help_text() -> str:
    return HELP


def run(s: store.Store, command: str, session_id: str | None = None) -> str:
    """Execute one control command against the locked store ``s``; return the reply.

    ``session_id`` lets ``show`` name the task's plan and handoff files.
    """
    words = (command or "").split(None, 2)
    name = words[0].lower() if words else "list"
    arg = words[1] if len(words) > 1 else None
    rest = words[2].strip() if len(words) > 2 else ""

    if name == "list":
        return _list(s)
    if name in ("show", "cancel", "promote", "park", "auto", "manual"):
        if arg is None:
            return f"usage: {name} pN"
        task_id = _task_id(arg)
        if name == "show":
            return _show(s, task_id, session_id)
        if name == "cancel":
            return _cancel(s, task_id)
        if name == "promote":
            return _promote(s, task_id)
        if name == "park":
            return _park(s, task_id)
        return _auto(s, task_id, auto=(name == "auto"))
    if name == "verify":
        if arg is None or not rest:
            return "usage: verify pN <command>"
        return _verify(s, _task_id(arg), rest)
    if name == "approve":
        return _approve(s, _task_id(arg) if arg is not None else None)
    if name == "drop":
        return _drop(s)
    if name == "pause":
        s.paused = True
        return "queue paused"
    if name == "resume":
        if arg is not None:
            return _promote(s, _task_id(arg))
        s.paused = False
        return "queue resumed"
    return help_text()
