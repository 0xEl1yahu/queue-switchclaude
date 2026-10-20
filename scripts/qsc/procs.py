"""Process-group helpers that differ between POSIX and Windows. Stdlib only.

POSIX: a child started with ``start_new_session=True`` leads its own
process group, and ``os.killpg`` kills the group. Windows has neither, so a
child is started with ``CREATE_NEW_PROCESS_GROUP`` and its whole tree is
killed with ``taskkill /F /T``. Callers pass ``group_kwargs()`` or
``detached_kwargs()`` to ``Popen`` and call ``kill_tree`` on timeout, and
the POSIX path stays exactly what it was before Windows support.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess

WINDOWS = os.name == "nt"

# How long ``taskkill`` itself may take before the kill is abandoned.
_TASKKILL_TIMEOUT = 5.0


def group_kwargs() -> dict:
    """``Popen`` keyword arguments that start the child in a new process group."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_kwargs() -> dict:
    """``Popen`` keyword arguments for a child that outlives its parent.

    On Windows ``DETACHED_PROCESS`` also gives the child no console, so no
    window flashes up while the hook returns.
    """
    if WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and everything it started, then ``proc`` itself.

    POSIX: SIGKILL the process group ``proc`` leads (its pid is the group id
    under ``start_new_session``). Windows: ``taskkill /F /T`` on its pid,
    which walks the tree by parent pid. Every failure is ignored: the tree
    may already be gone, and the hook must return either way.
    """
    if WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TASKKILL_TIMEOUT,
            )
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
