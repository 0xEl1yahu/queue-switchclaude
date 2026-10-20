"""The Bash side of the ``/queue`` command: ``queue_cli.py [--session ID] <command words>``.

Runs one ``qsc.control`` command (``list``, ``cancel pN``, ``promote pN``,
``park pN``, ``drop``, ``pause``, ``resume``; empty means ``list``) against
the session's store and prints the reply. The session comes from
``--session ID`` (or ``--session=ID``), else ``$QSC_SESSION``, else the
pointer intake keeps for the current directory
(``paths.cwd_pointer_path``), else the global pointer it rewrites on every
event (``paths.pointer_path()``).

Exit status is always 0: the command's output is the whole answer, and a
slash command that "fails" would only hide it. With no session to act on it
prints ``no session found``.

The ``queue:`` prefix in a prompt is the fast path (no model, no turn);
``/queue-switchclaude:queue`` is the discoverable path that costs one short
turn and ends here.
"""
from __future__ import annotations

import time; _T0 = time.monotonic()  # first statement: hook_ms counts the qsc imports too

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NO_SESSION = "no session found"
SESSION_FLAG = "--session"


def parse_args(argv: list[str]) -> tuple[str | None, str]:
    """Split ``argv`` into ``(session id or None, command string)``.

    ``--session ID`` and ``--session=ID`` are taken wherever they appear;
    every other word belongs to the command, in order.
    """
    session: str | None = None
    words: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == SESSION_FLAG and i + 1 < len(argv):
            session = argv[i + 1]
            i += 2
            continue
        if arg.startswith(SESSION_FLAG + "="):
            session = arg[len(SESSION_FLAG) + 1:]
            i += 1
            continue
        words.append(arg)
        i += 1
    session = (session or "").strip()
    return (session or None), " ".join(words)


def _read_pointer(path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.strip()
    return text or None


def resolve_session(explicit: str | None, cwd: str | None = None) -> str | None:
    """``--session``, else ``$QSC_SESSION``, else the pointer for ``cwd``
    (the current directory by default), else the global pointer, else None.

    The Bash tool runs the command in the project directory, and intake
    keys a pointer by each event's ``cwd``, so a session in another folder
    never answers for this one (M3 review focus 1). Both sides use the
    canonical spelling of the folder (``paths.canonical_cwd``) and the CLI
    also tries each parent directory, so a ``cd`` into a subdirectory or a
    symlinked path still finds this project's session. The global pointer
    is the fallback for a directory no session has typed in.
    """
    if explicit:
        return explicit
    env = os.environ.get("QSC_SESSION", "").strip()
    if env:
        return env
    from qsc import paths

    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = None
    if cwd:
        for folder in _cwd_chain(cwd):
            found = _read_pointer(paths.cwd_pointer_path(folder))
            if found:
                return found
    return _read_pointer(paths.pointer_path())


def _cwd_chain(cwd: str) -> list[str]:
    """``cwd`` (canonical) and then each of its parents up to the root.

    The Bash tool keeps its working directory across calls, so by the time
    the slash command runs it may sit in a subdirectory of the project the
    session typed in; the pointer for the nearest enclosing folder is that
    session's, and the global pointer stays the fallback for a folder no
    session has typed in.
    """
    from pathlib import Path

    from qsc import paths

    here = Path(paths.canonical_cwd(cwd))
    return [str(here)] + [str(parent) for parent in here.parents]


def main(argv: list[str]) -> str:
    """Run the command and return the text to print."""
    try:
        from qsc import control, hookio, log, paths, store

        explicit, command = parse_args(argv)
        session_id = resolve_session(explicit)
        if not session_id:
            return NO_SESSION
        from qsc import dispatch
        with store.locked(session_id) as s:
            before_state, before_pending = s.state, s.pending_task
            reply = control.run(s, command, session_id=session_id)
            approved = (
                before_state == "awaiting_approval"
                and s.state == "running"
                and s.running_task is not None
                and s.running_task == before_pending
            )
            if approved:
                # The slash command runs inside a Claude turn: the printed
                # reply is what Claude reads, so the execution handover rides
                # on it, on a line of its own (commands/queue.md tells Claude
                # to follow the line starting "queue-switchclaude: task").
                # As in intake's approve, that turn becomes the current one,
                # so messages typed during the execution are mid-turn.
                reply = f"{reply}\n{dispatch.start(s, s.get(s.running_task))}"
                s.current_prompt_id = _read_pointer(paths.last_prompt_path(session_id)) or s.current_prompt_id
                s.interrupt = False
        log.record(session_id, "control", command=command or "list", via="queue_cli",
                   approved=s.running_task if approved else None,
                   hook_ms=hookio.hook_ms({"_hook_started": _T0}))
        return reply
    except Exception as exc:  # the reply is the whole answer; never a traceback
        return f"queue-switchclaude: could not run the queue command ({type(exc).__name__}: {exc})"


if __name__ == "__main__":
    try:
        if os.name == "nt":  # the console code page cannot print every task text
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(main(sys.argv[1:]))
    except BaseException:
        pass
    sys.exit(0)
