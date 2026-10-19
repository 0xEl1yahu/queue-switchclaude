"""The ``queue:`` control channel. Stdlib only.

``run(s, command)`` executes one user command against an already-locked
store and returns the text intake shows the user. The rule table turns a
``queue: ...`` or ``/queue ...`` prompt into the bare command (``list``,
``cancel p2``, ...); an empty command means ``list``. Control never calls
the classifier and never counts as a turn, so every reply is short enough
to sit above the input box.

Commands (first word, case-insensitive):

    list          the running, queued, parked and paused tasks, on ONE line
                  (the mid-run notification shows a single line)
    cancel pN     a queued, parked or paused task becomes ``cancelled``
    promote pN    a parked or paused task becomes ``queued``
    park pN       a queued task becomes ``parked``
    drop          every queued and parked task becomes ``dropped``
    pause         ``Store.paused = True``: dispatch runs nothing
    resume        ``Store.paused = False``
    resume pN     a paused task becomes ``queued`` (same as promote)
    help          this list; so does any unknown word
"""
from __future__ import annotations

from . import store

TEXT_WIDTH = 60
ITEM_WIDTH = 36
COMMANDS = ("list", "cancel", "promote", "park", "drop", "pause", "resume", "help")

HELP = (
    "commands: list | cancel pN | promote pN (parked or paused) | park pN | "
    "drop | pause | resume [pN] | help"
)


def _clip(text: str) -> str:
    """One line of at most TEXT_WIDTH characters, with an ellipsis when cut."""
    flat = " ".join(text.split())
    if len(flat) <= TEXT_WIDTH:
        return flat
    return flat[:TEXT_WIDTH] + "…"


def _item(t: store.Task) -> str:
    """``p2 queued after p1: add a goodbye.py…`` with the text cut to ITEM_WIDTH."""
    after = f" after {', '.join(t.depends_on)}" if t.depends_on else ""
    flat = " ".join(t.text.split())
    if len(flat) > ITEM_WIDTH:
        flat = flat[:ITEM_WIDTH] + "…"
    return f"{t.id} {t.status}{after}: {flat}"


def _list(s: store.Store) -> str:
    """One line: the notification area shows a single line of a systemMessage."""
    live = s.candidates()
    body = "; ".join(_item(t) for t in live) if live else "queue is empty"
    if s.paused:
        body += "; queue is paused, send 'queue: resume' to dispatch again"
    return body


def _task_id(arg: str) -> str:
    """Accept ``p2``, ``P2`` or a bare ``2``."""
    arg = arg.strip().lower()
    return f"p{arg}" if arg.isdigit() else arg


def _cancel(s: store.Store, task_id: str) -> str:
    if s.get(task_id) is None:
        return f"no task {task_id}"
    t = s.cancel(task_id)
    if t is None:
        return f"{task_id} is not queued, parked or paused"
    return f"cancelled {t.id}: {t.text}"


def _promote(s: store.Store, task_id: str) -> str:
    if s.get(task_id) is None:
        return f"no task {task_id}"
    t = s.promote(task_id)
    if t is None:
        return f"{task_id} is not parked or paused"
    return f"promoted {t.id} to the queue"


def _park(s: store.Store, task_id: str) -> str:
    t = s.get(task_id)
    if t is None:
        return f"no task {task_id}"
    if t.status != "queued":
        return f"{task_id} is not queued"
    t.status = "parked"
    return f"parked {t.id}"


def _drop(s: store.Store) -> str:
    dropped = [t for t in s.tasks if t.status in ("queued", "parked")]
    for t in dropped:
        t.status = "dropped"
    n = len(dropped)
    noun = "task" if n == 1 else "tasks"
    if not dropped:
        return f"dropped 0 {noun}"
    return f"dropped {n} {noun}: " + ", ".join(t.id for t in dropped)


def help_text() -> str:
    return HELP


def run(s: store.Store, command: str) -> str:
    """Execute one control command against the locked store ``s``; return the reply."""
    words = (command or "").split()
    name = words[0].lower() if words else "list"
    arg = words[1] if len(words) > 1 else None

    if name == "list":
        return _list(s)
    if name in ("cancel", "promote", "park"):
        if arg is None:
            return f"usage: {name} pN"
        task_id = _task_id(arg)
        if name == "cancel":
            return _cancel(s, task_id)
        if name == "promote":
            return _promote(s, task_id)
        return _park(s, task_id)
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
