"""Per-session JSON store: one file per session, file lock, atomic save.

Stdlib only. The store lives at ``paths.store_path(session_id)``. A missing,
unparseable or wrongly shaped file is treated as a fresh store so a corrupt
file can never crash a hook.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import log, paths

TASK_STATUSES = ("queued", "running", "done", "interrupted", "dropped")
STORE_STATES = ("idle", "running")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Task:
    id: str
    text: str
    status: str = "queued"
    depends_on: list[str] = field(default_factory=list)
    prompt_id: str | None = None
    created_at: str = field(default_factory=_now_iso)


@dataclass
class Store:
    session_id: str
    state: str = "idle"
    current_prompt_id: str | None = None
    running_task: str | None = None
    next_id: int = 1
    interrupt: bool = False
    last_dispatched: str | None = None
    paused: bool = False  # set by `queue: pause`; dispatch runs nothing while True
    tasks: list[Task] = field(default_factory=list)

    def new_task(self, text: str, depends_on: list[str], prompt_id: str | None) -> Task:
        """Append a queued task with the next id ``p{next_id}``; ids are never reused."""
        task = Task(
            id=f"p{self.next_id}",
            text=text,
            status="queued",
            depends_on=list(depends_on),
            prompt_id=prompt_id,
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

        A dependency that names no known task is never satisfied.
        """
        done = {t.id for t in self.tasks if t.status == "done"}
        return [
            t for t in self.tasks
            if t.status == "queued" and all(dep in done for dep in t.depends_on)
        ]


def _from_dict(session_id: str, data: object) -> Store:
    """Build a Store from parsed JSON; raises KeyError/TypeError on a bad shape."""
    if not isinstance(data, dict):
        raise TypeError("store file is not a JSON object")
    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, list):
        raise TypeError("tasks is not a list")
    tasks = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise TypeError("task is not a JSON object")
        tasks.append(Task(**raw))
    fields = {k: v for k, v in data.items() if k not in ("tasks", "session_id")}
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
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _lock(path: Path) -> Iterator[None]:
    """Hold an exclusive ``fcntl.flock`` on ``<path>.lock`` for the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


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
