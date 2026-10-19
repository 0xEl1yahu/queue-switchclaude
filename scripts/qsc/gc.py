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

Stale pointers (M4 Task 3): intake writes a per-directory pointer under
``pointers/`` on every event, before it knows whether the message needs a
store, so a session whose only message was a pass-through leaves a pointer
and nothing else, and a data dir wiped by hand leaves pointers naming
sessions that no longer exist. ``collect`` also removes every file under
``pointers/`` that names a session with no store, log, lock or directory,
except one naming a session in ``protect``: the running session may well
have written its pointer and nothing else yet (M4 review focus 3). The
global ``last_session`` pointer is only dropped when it names a deleted
session, as before. ``collect`` returns ``(deleted_sessions,
deleted_pointers)``, both sorted.

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


def _drop_global_pointer(deleted: set[str]) -> None:
    """Remove ``last_session`` when it still names a deleted session."""
    pointer = paths.pointer_path()
    try:
        named = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if named and paths.safe_id(named) in deleted:
        with contextlib.suppress(OSError):
            pointer.unlink()


def _drop_stale_pointers(protected: set[str]) -> list[str]:
    """Remove every file under ``pointers/`` that names no session on disk;
    return the names removed, sorted.

    A pointer is stale when it is empty or names a session with no store,
    log, lock or directory (``_session_ids`` after this run's deletions, so
    the pointers of just-deleted sessions go too). A session in
    ``protected`` keeps its pointer whatever is on disk: the running
    session's first message may have been a pass-through, which writes the
    pointer and nothing else (M4 review focus 3). Hidden names and the temp
    files of an atomic write in progress are skipped, as is anything that
    is not a plain file.
    """
    live = _session_ids() | protected
    removed: list[str] = []
    try:
        entries = list(paths.pointers_dir().iterdir())
    except OSError:
        return removed
    for pointer in entries:
        name = pointer.name
        if name.startswith(".") or _TMP_MARK in name or not pointer.is_file():
            continue
        try:
            named = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if named and paths.safe_id(named) in live:
            continue
        try:
            pointer.unlink()
        except OSError:
            continue
        removed.append(name)
    return sorted(removed)


def collect(
    keep_days: int = KEEP_DAYS,
    now: float | None = None,
    protect: set[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Delete every session untouched for more than ``keep_days`` and every
    stale pointer; return ``(deleted_sessions, deleted_pointers)``, the
    session ids and the ``pointers/`` file names removed, each sorted.

    ``now`` is an epoch timestamp (``time.time()`` by default). ``protect``
    holds session ids (as the hooks see them, before ``paths.safe_id``) that
    are never deleted and whose pointers are never dropped. A session is
    deleted only when its newest file is strictly older than the window;
    anything at or inside it stays.
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
        _drop_global_pointer(set(deleted))
    pointers = _drop_stale_pointers(protected)
    return deleted, pointers
