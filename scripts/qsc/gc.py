"""Session garbage collection: forget sessions nobody has touched in a month.

Stdlib only. Every Claude Code session leaves a store (``sessions/<id>.json``
and its ``.lock``), a decision log (``log/<id>.jsonl``) and, once a task was
planned or finished, a directory (``sessions/<id>/`` with plans and
handoffs). Nothing ever removed them, so the data dir grew by one session
per conversation (M3 Task 2).

``collect`` deletes the files of every session whose **newest** file is
older than ``keep_days``. Three guards (plan review focus 4):

* a session named in ``protect`` is never touched, whatever its age; dispatch
  passes the session it runs in;
* nothing younger than the window goes: the newest file decides, so one
  fresh plan keeps an old store, and a clock that is wrong by minutes cannot
  matter against a window of days (a file dated in the future is young);
* a session's files are deleted as a unit, store, lock, log and directory,
  and a pointer file that still names a deleted session is dropped with it,
  so the CLI never resolves to a session that no longer exists.

The global constraint that one session's hooks never touch another
session's files holds for live sessions: what ``collect`` removes has not
been written for ``keep_days``, long after any hook of that session could
still run. Dispatch calls it once per session and swallows every error;
``collect`` itself never raises on an unreadable or vanishing file.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

from . import paths

KEEP_DAYS = 30
_LOCK_SUFFIX = ".lock"
_TMP_MARK = ".tmp"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _newest_under(root: Path) -> float | None:
    """The newest mtime of ``root`` and everything below it, or None when it
    does not exist."""
    newest = _mtime(root)
    if newest is None:
        return None
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
        for name in dirnames + filenames:
            m = _mtime(Path(dirpath) / name)
            if m is not None and m > newest:
                newest = m
    return newest


def _session_ids() -> set[str]:
    """Every safe session id that has a store, a log or a session directory."""
    ids: set[str] = set()
    sessions = paths.data_dir() / "sessions"
    logs = paths.data_dir() / "log"
    with contextlib.suppress(OSError):
        for entry in sessions.iterdir():
            name = entry.name
            if name.startswith(".") or _TMP_MARK in name:
                continue  # a temp file of an atomic write in progress
            if entry.is_dir():
                ids.add(name)
            elif name.endswith(".json"):
                ids.add(name[: -len(".json")])
            elif name.endswith(".json" + _LOCK_SUFFIX):
                ids.add(name[: -len(".json" + _LOCK_SUFFIX)])
    with contextlib.suppress(OSError):
        for entry in logs.iterdir():
            name = entry.name
            if name.startswith(".") or not name.endswith(".jsonl"):
                continue
            ids.add(name[: -len(".jsonl")])
    return ids


def _files_of(session_id: str) -> tuple[Path, Path, Path, Path]:
    """``(store, lock, log, directory)`` for a session."""
    store = paths.store_path(session_id)
    return store, store.with_name(store.name + _LOCK_SUFFIX), paths.log_path(session_id), paths.session_dir(session_id)


def newest_mtime(session_id: str) -> float | None:
    """The mtime of the session's newest file, or None when it has none.

    The lock file counts only when it is all that is left: ``flock`` does
    not touch its mtime, so next to a store it says nothing the store does
    not, but an orphan lock (a session that never saved a store) should
    still age out.
    """
    store, lock, log, directory = _files_of(session_id)
    times = [m for m in (_mtime(store), _mtime(log), _newest_under(directory)) if m is not None]
    if not times:
        return _mtime(lock)
    return max(times)


def _delete(session_id: str) -> bool:
    """Remove the session's files; True when nothing of it is left."""
    store, lock, log, directory = _files_of(session_id)
    ok = True
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
        ok = ok and not directory.exists()
    for path in (store, log, lock):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            ok = False
    return ok


def _drop_pointers(deleted: set[str]) -> None:
    """Remove pointer files that still name a deleted session."""
    candidates = [paths.pointer_path()]
    with contextlib.suppress(OSError):
        candidates.extend(p for p in paths.pointers_dir().iterdir() if p.is_file())
    for pointer in candidates:
        try:
            named = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if named and paths.safe_id(named) in deleted:
            with contextlib.suppress(OSError):
                pointer.unlink()


def collect(
    keep_days: int = KEEP_DAYS,
    now: float | None = None,
    protect: set[str] = frozenset(),
) -> list[str]:
    """Delete every session untouched for more than ``keep_days``; return
    the deleted ids, sorted.

    ``now`` is an epoch timestamp (``time.time()`` by default). ``protect``
    holds session ids (as the hooks see them, before ``paths.safe_id``) that
    are never deleted. A session is deleted only when its newest file is
    strictly older than the window; anything at or inside it stays.
    """
    if now is None:
        now = time.time()
    try:
        window = max(float(keep_days), 0.0) * 86400.0
    except (TypeError, ValueError):
        window = KEEP_DAYS * 86400.0
    protected = {paths.safe_id(p) for p in protect}
    deleted: list[str] = []
    for sid in sorted(_session_ids()):
        if sid in protected:
            continue
        newest = newest_mtime(sid)
        if newest is None or (now - newest) <= window:
            continue
        if _delete(sid):
            deleted.append(sid)
    if deleted:
        _drop_pointers(set(deleted))
    return deleted
