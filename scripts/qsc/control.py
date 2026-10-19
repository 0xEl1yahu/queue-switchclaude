"""The ``queue:`` control channel. Stdlib only.

``run(s, command)`` executes one user command against an already-locked
store and returns the text intake shows the user. The rule table turns a
``queue: ...`` or ``/queue ...`` prompt into the bare command (``list``,
``cancel p2``, ...); an empty command means ``list``. Control never calls
the classifier and never counts as a turn, so every reply is short enough
to sit above the input box.

Commands (first word, case-insensitive):

    list          one line per running, queued, parked or paused task
    cancel pN     a queued, parked or paused task becomes ``cancelled``
    promote pN    a parked task becomes ``queued``
    park pN       a queued task becomes ``parked``
    drop          every queued and parked task becomes ``dropped``
    pause         ``Store.paused = True``: dispatch runs nothing
    resume        ``Store.paused = False``
    help          this list; so does any unknown word
"""
from __future__ import annotations

from . import store

TEXT_WIDTH = 60
COMMANDS = ("list", "cancel", "promote", "park", "drop", "pause", "resume", "help")

HELP_LINES = (
    "list: show the running, queued, parked and paused tasks",
    "cancel pN: cancel a queued, parked or paused task",
    "promote pN: move a parked task into the queue",
    "park pN: move a queued task aside until you promote it",
    "drop: drop every queued and parked task",
    "pause: stop dispatching queued tasks until resume",
    "resume: dispatch queued tasks again",
    "help: this list",
)


def _clip(text: str) -> str:
    """One line of at most TEXT_WIDTH characters, with an ellipsis when cut."""
    flat = " ".join(text.split())
    if len(flat) <= TEXT_WIDTH:
        return flat
    return flat[:TEXT_WIDTH] + "…"


def _line(t: store.Task) -> str:
    after = f"after {', '.join(t.depends_on)}" if t.depends_on else ""
    head = f"{t.id:<4}{t.status:<9}"
    if after:
        head += f"{after:<11}"
    return head + _clip(t.text)


def _list(s: store.Store) -> str:
    live = s.candidates()
    body = "\n".join(_line(t) for t in live) if live else "queue is empty"
    if s.paused:
        body += "\nqueue is paused; send 'queue: resume' to dispatch again"
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
        return f"{task_id} is not parked"
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
    return "\n".join(HELP_LINES)


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
        s.paused = False
        return "queue resumed"
    return help_text()
