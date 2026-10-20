"""Per-session JSON store: one file per session, file lock, atomic save.

Stdlib only. The store lives at ``paths.store_path(session_id)``. A missing,
unparseable or wrongly shaped file is treated as a fresh store so a corrupt
file can never crash a hook.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import log, paths

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# Windows: how often a waiting writer retries the lock byte, and how long
# ``os.replace`` keeps retrying while a reader still has the store open
# (Windows refuses to replace a file another process holds open).
_LOCK_POLL = 0.02
_REPLACE_TRIES = 50

TASK_STATUSES = (
    "queued", "parked", "running", "paused", "done", "interrupted",
    "dropped", "cancelled", "shadow_queued", "shadow_dispatched",
    # v3: a plan was written and awaits approval; a verify command failed
    # and the task was handed back (promote returns it to the queue).
    "planned", "failed_verify",
)
# v3: `planning` while Claude writes a plan for `pending_task`;
# `awaiting_approval` once the plan is on screen; `awaiting_answer` when the
# running task ended by asking the user something.
STORE_STATES = ("idle", "running", "planning", "awaiting_approval", "awaiting_answer")

# Statuses a task can be cancelled from (control "cancel pN", amend note "cancel").
CANCELLABLE = ("queued", "parked", "paused", "planned", "failed_verify")
# Statuses promote() moves back to `queued`.
PROMOTABLE = ("parked", "paused", "failed_verify")
# Statuses that make a task a live candidate for amend targets and listings.
CANDIDATE_STATUSES = ("queued", "parked", "paused", "running", "planned", "failed_verify")
# Statuses find_recent() never matches: the task is gone, so resubmitting
# the same text is a fresh request, not a duplicate.
_NOT_RECENT = ("dropped", "cancelled")

_CURLY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_TRAILING_PUNCT = ".,;:!?"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    """A timezone-aware datetime from a store timestamp, or None when unreadable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalise(text: str) -> str:
    """The comparison form of a task text: lowercase, curly quotes straightened,
    whitespace collapsed, trailing ``.,;:!?`` stripped.

    Dedupe compares on this (plan: exact after normalisation only), so
    "later: add goodbye.py" and "later: add a goodbye.py" stay distinct.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    collapsed = " ".join(text.translate(_CURLY).split()).lower()
    return collapsed.rstrip(_TRAILING_PUNCT + " ")


@dataclass
class Task:
    id: str
    text: str
    status: str = "queued"
    depends_on: list[str] = field(default_factory=list)
    prompt_id: str | None = None
    created_at: str = field(default_factory=_now_iso)
    # v2: filled by Store.new_task via normalise(); "" in stores written by M0.
    norm_text: str = ""
    # v2: amend appendices ("the webhook handler should verify signatures").
    notes: list[str] = field(default_factory=list)
    # v3: shell command that decides done (exit 0) instead of the judge.
    verify: str | None = None
    # v3: skip the approval gate; the plan flows straight into execution.
    auto: bool = False
    # v3: "not done, continue" / "verify failed" handbacks so far (cap 2),
    # and planning retries while in state `planning`.
    attempts: int = 0
    # v3: ids of the tasks that were `done` when the plan was written, so
    # Store.stale_planned() can tell a plan whose dependency finished later.
    plan_based_on: list[str] = field(default_factory=list)
    # v3: the closing message filed when the task was marked done.
    closing: str | None = None
    # v3: verify_ok | verify_failed | done | not_done | asked_question |
    # gave_up | stopped (and `confirmed` when the user confirms a task done).
    verdict: str | None = None
    # M3: "prompt" for a turn the user typed (registered so queued work can
    # chain after it), "task" for work the harness queued. Only tasks are
    # judged; a prompt turn closes as `stopped` without a model call.
    kind: str = "task"


@dataclass
class Store:
    session_id: str
    state: str = "idle"
    current_prompt_id: str | None = None
    running_task: str | None = None
    next_id: int = 1
    interrupt: bool = False
    # M0/M1: the id of the task dispatch handed over last. Unused since M3
    # (the guard it fed could never fire); kept so older store files load.
    last_dispatched: str | None = None
    paused: bool = False  # set by `queue: pause`; dispatch runs nothing while True
    # v3: the task being planned, awaiting approval or awaiting an answer.
    pending_task: str | None = None
    # M3: consecutive Stop-hook continuations the harness has issued; reset
    # by any Stop that is not a continuation. Bounds automatic chains.
    continuations: int = 0
    # M3: sha1 of the last handover text dispatch issued. A Stop-hook
    # continuation whose handover hashes the same is a spin and is suppressed.
    last_handback: str | None = None
    # M3: when dispatch ran session garbage collection (``qsc.gc``) on this
    # session's behalf; None until it has. At most one run per session.
    gc_at: str | None = None
    tasks: list[Task] = field(default_factory=list)

    def new_task(
        self,
        text: str,
        depends_on: list[str],
        prompt_id: str | None,
        status: str = "queued",
        kind: str = "task",
    ) -> Task:
        """Append a task with the next id ``p{next_id}``; ids are never reused.

        ``status`` defaults to ``queued``; intake passes ``parked`` for a
        maybe-later item and ``shadow_queued`` in shadow mode.
        """
        task = Task(
            id=f"p{self.next_id}",
            text=text,
            status=status,
            depends_on=list(depends_on),
            prompt_id=prompt_id,
            norm_text=normalise(text),
            kind=kind,
        )
        self.next_id += 1
        self.tasks.append(task)
        return task

    def get(self, task_id: str) -> Task | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def ready(self) -> list[Task]:
        """Queued tasks whose every dependency is done, in creation order.

        A dependency that names no known task is never satisfied. Parked and
        paused tasks are never ready: only ``queued`` counts.
        """
        done = set(self.done_ids())
        return [
            t for t in self.tasks
            if t.status == "queued" and all(dep in done for dep in t.depends_on)
        ]

    def done_ids(self) -> list[str]:
        """Ids of every ``done`` task, in creation order."""
        return [t.id for t in self.tasks if t.status == "done"]

    def stale_planned(self) -> list[Task]:
        """``planned`` tasks whose plan predates the completion of one of their own
        dependencies.

        A plan records ``plan_based_on`` (the ``done`` ids at the time it was
        written). Restricted to the task's ``depends_on``, that set is compared
        with the current ``done_ids()``; a difference means a dependency
        finished after the plan was written and its outcome may have changed
        what the plan assumed, so dispatch re-plans it. Tasks that finished
        but are not dependencies never make a plan stale.
        """
        done_now = set(self.done_ids())
        stale = []
        for task in self.tasks:
            if task.status != "planned":
                continue
            deps = set(task.depends_on)
            if (done_now & deps) != (set(task.plan_based_on) & deps):
                stale.append(task)
        return stale

    def find_recent(
        self,
        norm_text: str,
        within_seconds: int,
        now: datetime | None = None,
    ) -> Task | None:
        """The newest task with equal ``norm_text`` created within the window.

        Dropped and cancelled tasks never match. A task whose ``created_at``
        cannot be parsed is skipped rather than guessed at.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        for task in reversed(self.tasks):
            if task.norm_text != norm_text or task.status in _NOT_RECENT:
                continue
            created = _parse_iso(task.created_at)
            if created is None:
                continue
            if 0 <= (now - created).total_seconds() <= within_seconds:
                return task
        return None

    def cancel(self, task_id: str) -> Task | None:
        """Mark a queued, parked, paused, planned or failed_verify task
        ``cancelled``; else ``None``."""
        task = self.get(task_id)
        if task is None or task.status not in CANCELLABLE:
            return None
        task.status = "cancelled"
        return task

    def promote(self, task_id: str) -> Task | None:
        """Move a parked, paused or failed_verify task back to the queue;
        ``None`` otherwise.

        A paused task (amend note ``pause``, or the running task left paused
        by dispatch) has no other way back into ``ready()``; neither has a
        task whose verify command kept failing until dispatch gave it back.
        """
        task = self.get(task_id)
        if task is None or task.status not in PROMOTABLE:
            return None
        task.status = "queued"
        return task

    def candidates(self) -> list[Task]:
        """Live tasks (queued, parked, paused, running, planned, failed_verify)
        in creation order.

        These are the amend targets and what ``queue: list`` shows.
        """
        return [t for t in self.tasks if t.status in CANDIDATE_STATUSES]


_TASK_FIELDS = frozenset(f.name for f in dataclasses.fields(Task))
_STORE_FIELDS = frozenset(f.name for f in dataclasses.fields(Store)) - {"session_id", "tasks"}


def _from_dict(session_id: str, data: object) -> Store:
    """Build a Store from parsed JSON; raises KeyError/TypeError on a bad shape.

    Unknown keys on the store or a task are ignored, so a file written by a
    newer version still loads. A missing required key (``tasks``, a task's
    ``id`` or ``text``) is still a bad shape.
    """
    if not isinstance(data, dict):
        raise TypeError("store file is not a JSON object")
    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, list):
        raise TypeError("tasks is not a list")
    tasks = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise TypeError("task is not a JSON object")
        tasks.append(Task(**{k: v for k, v in raw.items() if k in _TASK_FIELDS}))
    fields = {k: v for k, v in data.items() if k in _STORE_FIELDS}
    return Store(session_id=session_id, tasks=tasks, **fields)


def load(session_id: str) -> Store:
    """Read the session's store; a missing or unreadable file yields a fresh Store.

    A file that exists but cannot be parsed or has the wrong shape is also
    treated as fresh, and the corruption is written to the decision log so
    the silent reset is visible (plan review focus 4).
    """
    path = paths.store_path(session_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return _from_dict(session_id, data)
    except FileNotFoundError:
        return Store(session_id=session_id)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        log.record(
            session_id, "store_corrupt",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return Store(session_id=session_id)


def _write(store: Store, path: Path) -> None:
    """Write ``store`` to ``path`` via a sibling temp file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dataclasses.asdict(store), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _replace(tmp: str, path: Path) -> None:
    """``os.replace``, retried on Windows while another process has ``path`` open.

    A reader (``load`` outside the lock, ``queue: list``) holds the file
    only for a moment, so the retry is short; the last failure is raised.
    """
    if os.name != "nt":
        os.replace(tmp, path)
        return
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_LOCK_POLL)


@contextlib.contextmanager
def _lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``<path>.lock`` for the block.

    POSIX: ``fcntl.flock``. Windows: ``msvcrt.locking`` on byte 0 of the
    lock file, polled with ``LK_NBLCK`` until it is free, so a writer waits
    as long as it would on ``flock`` (``LK_LOCK`` gives up after 10 s).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as lock_fh:
        if os.name == "nt":
            _lock_byte(lock_fh.fileno())
            try:
                yield
            finally:
                os.lseek(lock_fh.fileno(), 0, os.SEEK_SET)
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _lock_byte(fd: int) -> None:
    """Windows: take the lock on byte 0 of ``fd``, waiting until it is free.

    ``msvcrt.locking`` locks from the current position, and ``a+`` opens at
    the end of the file, so the position is reset first. Locking past the
    end of an empty file is allowed.
    """
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(_LOCK_POLL)


def save(store: Store) -> None:
    """Atomically write the store, holding the session lock for the whole write."""
    path = paths.store_path(store.session_id)
    with _lock(path):
        _write(store, path)


@contextlib.contextmanager
def locked(session_id: str) -> Iterator[Store]:
    """Load the store under the session lock, yield it, and save on normal exit.

    An exception inside the block leaves the file as it was.
    """
    path = paths.store_path(session_id)
    with _lock(path):
        store = load(session_id)
        yield store
        _write(store, path)
