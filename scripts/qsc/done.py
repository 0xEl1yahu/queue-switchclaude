"""Done detection on Stop: a verify command if the task has one, else a judge.

Stdlib only. Decision D4 (spec section 7): a task may carry a verify command;
exit 0 on Stop means done. Otherwise a haiku judge runs through ``model.call``
over the closing message (``last_assistant_message``) and the plan excerpt,
answering ``done`` / ``not_done`` / ``asked_question``.

Every branch fails open so the queue keeps moving: a verify command that
hangs is ``verify_failed`` after its timeout (dispatch hands the tail back,
then gives up after two attempts); a judge that is unavailable or answers
outside its contract is ``done``; an empty closing message is ``stopped``,
which counts as done.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import model, store

# Every verdict dispatch can file on a task (`Task.verdict`).
VERDICTS = ("verify_ok", "verify_failed", "done", "not_done", "asked_question", "gave_up", "stopped")

# What the judge itself may answer; the rest come from the verify runner,
# from dispatch (`gave_up`) and from an empty closing message (`stopped`).
JUDGE_VERDICTS = ("done", "not_done", "asked_question")

# A `not_done` below this confidence is treated as `done`: conservative in
# the direction of not nagging the user with a "finish it" handback.
NOT_DONE_THRESHOLD = 0.7

# The last this many characters of a failed verify command's combined output
# travel back to Claude in the handback.
TAIL_CHARS = 1500

VERIFY_TIMEOUT = 120.0

# scripts/qsc/done.py -> scripts/ -> repo root -> prompts/judge_system.md
JUDGE_PROMPT = Path(__file__).resolve().parent.parent.parent / "prompts" / "judge_system.md"

JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(JUDGE_VERDICTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reason"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    """What done detection decided and why.

    ``output_tail`` carries the end of a failed verify command's output so
    dispatch can hand it back to Claude; it is empty for every other verdict.
    """
    verdict: str
    reason: str
    output_tail: str = ""


def run_verify(command: str, cwd: str | None, timeout: float = VERIFY_TIMEOUT) -> Verdict:
    """Run ``command`` through the shell in ``cwd``; exit 0 is ``verify_ok``.

    Any other exit is ``verify_failed`` with ``reason = "exit {code}"`` and
    the last TAIL_CHARS characters of stdout+stderr in ``output_tail``. A
    command that outlives ``timeout`` is ``verify_failed`` with reason
    ``timeout after {timeout} s`` (review focus 3: the Stop hook returns
    within its own budget). A command that cannot be started at all (bad
    ``cwd``, no shell) is also ``verify_failed`` so the task is handed back
    rather than silently closed.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        return Verdict("verify_failed", f"timeout after {timeout:g} s", _tail(exc.stdout, exc.stderr))
    except (OSError, ValueError) as exc:
        return Verdict("verify_failed", f"could not run: {type(exc).__name__}: {exc}")
    if proc.returncode == 0:
        return Verdict("verify_ok", "exit 0")
    return Verdict("verify_failed", f"exit {proc.returncode}", _tail(proc.stdout, proc.stderr))


def _tail(stdout: object, stderr: object) -> str:
    """The last TAIL_CHARS characters of stdout followed by stderr."""
    combined = _text(stdout) + _text(stderr)
    return combined[-TAIL_CHARS:]


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 1:
        return None
    return float(value)


def judge(
    task_text: str,
    notes: list[str],
    plan_excerpt: str,
    closing: str,
    claude: str = "claude",
) -> Verdict:
    """Ask the haiku judge whether ``closing`` says the task's work is complete.

    The payload is ``{task, notes, plan, closing}``; the answer is validated
    against JUDGE_SCHEMA by hand. ``None`` from ``model.call`` or an answer
    outside the contract fails open as ``done`` (``judge unavailable; fail
    open``) so the queue keeps moving; the log carries ``judge: null`` in
    that case. A ``not_done`` with confidence below NOT_DONE_THRESHOLD is
    treated as ``done``.
    """
    payload = {
        "task": task_text,
        "notes": list(notes),
        "plan": plan_excerpt,
        "closing": closing,
    }
    out = model.call(JUDGE_PROMPT, JUDGE_SCHEMA, payload, 15.0, claude)
    if out is None:
        return Verdict("done", "judge unavailable; fail open")
    verdict = out.get("verdict")
    confidence = _confidence(out.get("confidence"))
    reason = out.get("reason")
    if verdict not in JUDGE_VERDICTS or confidence is None or not isinstance(reason, str):
        return Verdict("done", "judge unavailable; fail open")
    if verdict == "not_done" and confidence < NOT_DONE_THRESHOLD:
        return Verdict("done", f"not_done at {confidence:.2f} below {NOT_DONE_THRESHOLD}: {reason}")
    return Verdict(verdict, reason)


def decide(task: store.Task, event: dict, plan_excerpt: str, claude: str = "claude") -> Verdict:
    """Is ``task`` done, given the Stop ``event``?

    The verify command wins when the task has one (``run_verify`` in the
    event's ``cwd``). Otherwise the judge sees ``last_assistant_message``;
    an empty closing message is ``stopped`` (``no closing message``), which
    dispatch counts as done. The judge is looked up on this module at call
    time so tests and callers can monkeypatch ``done.judge``.
    """
    if task.verify:
        cwd = event.get("cwd")
        return run_verify(task.verify, cwd if isinstance(cwd, str) and cwd else None)
    closing = event.get("last_assistant_message")
    if not isinstance(closing, str) or not closing.strip():
        return Verdict("stopped", "no closing message")
    return judge(task.text, list(task.notes), plan_excerpt, closing, claude=claude)
