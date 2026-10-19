"""Stop-hook dispatch: the plan-flow state machine (spec sections 4.3, 7; D3, D4).

Stdlib only. Every Stop runs up to three stages, one function each:

* ``_close_running``: in state ``running``, decide whether the task is done
  (``done.decide``: its verify command, else the judge). A failed verify or
  a ``not_done`` is handed back at most twice (``Task.attempts``); a question
  to the user parks the store in ``awaiting_answer``; anything else closes
  the task and files its handoff (``write_handoff``).
* ``_after_planning``: in state ``planning``, record the plan Claude wrote
  (``planned``, then ``awaiting_approval`` or, for an ``auto`` task, straight
  into execution through ``start``). A missing plan is asked for once more,
  then the task goes back to ``queued`` (review focus 2).
* ``choose_next``: from ``idle`` (or after a task closed), re-plan stale
  children, honour ``paused`` and shadow mode, re-present a planned task that
  is waiting for approval (review focus 1), else hand the next ready task
  over for planning.

A Stop in ``awaiting_approval`` or ``awaiting_answer`` changes nothing: the
user's turn ran something else and intake already decided what that meant.

Every handover goes through ``hookSpecificOutput.additionalContext``
(``handback``): a ``decision: "block"`` would also keep the turn alive but
shows as a hook error in the UI. A Stop-hook continuation keeps the original
``current_prompt_id``, so intake still sees later messages as mid-turn.

Spin guard (M3): ``handle`` hashes every handover it issues into
``Store.last_handback``. A Stop fired by the harness's own continuation
(``stop_hook_active``) whose handover hashes the same as the last one is a
spin, so it is suppressed with a ``systemMessage`` and the turn ends; the
user's next message carries on. The guard covers the instruction handovers
(planning, its retry, re-presenting a plan, execution). The "fix it" and
"finish it" handbacks from ``_close_running`` are bounded by ``Task.attempts``
instead: their second copy is by design, and ends in ``failed_verify`` or
``gave_up``.
"""
from __future__ import annotations

import contextlib
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import done, gc, hookio, log, paths, store
from .interrupt import reset_after_interrupt

PREFIX = "queue-switchclaude"

SHADOW_QUEUED = "shadow_queued"
SHADOW_DISPATCHED = "shadow_dispatched"
_SATISFIED = ("done", SHADOW_DISPATCHED)

# "Not done, continue" and "verify failed" handbacks per task before dispatch
# gives up and closes it with verdict `gave_up`; and planning turns that end
# without a plan file before the task returns to the queue. Well under the
# Stop-hook continuation cap of 8.
MAX_ATTEMPTS = 2
# Consecutive Stop-hook continuations the harness may issue before it stops
# driving the chain and waits for the user (Claude Code's own cap is 8).
MAX_CONTINUATIONS = 6

# How much of the plan file the judge sees.
PLAN_EXCERPT_CHARS = 1500

_WAITING = ("awaiting_approval", "awaiting_answer")


# --- envelopes and handover texts --------------------------------------------


def _record(session_id: str, event_name: str, **fields) -> None:
    """``log.record`` plus ``hook_ms``, the time since the hook parsed its event."""
    log.record(session_id, event_name, hook_ms=hookio.hook_ms(), **fields)


def handback(text: str, system_message: str | None = None) -> dict:
    """The Stop output that keeps the turn alive with ``text`` as context.

    ``system_message`` is shown to the user (the universal hook field).
    """
    out: dict = {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}}
    if system_message:
        out["systemMessage"] = system_message
    return out


def _failed_verify_message(task: store.Task) -> str:
    return (
        f"{PREFIX}: {task.id} failed its verify command {MAX_ATTEMPTS} times and is set aside as "
        f"failed_verify (send 'queue: promote {task.id}' to retry it, 'queue: cancel {task.id}' to drop it)"
    )


def _budget_message(n: int) -> str:
    return f"{PREFIX}: paused the automatic chain after {n} continuations; send any message to carry on"


SPIN_GUARD_MESSAGE = (
    f"{PREFIX}: the same instruction came back twice; stopping here, send any message to continue"
)


def _digest(text: str) -> str:
    """The sha1 hex digest ``Store.last_handback`` keeps for a handover text."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _notes(task: store.Task) -> str:
    """``. The user later added: n1; n2`` or ``""``.

    An amend by subject (``intake._amend``) appends to ``task.notes`` rather
    than rewriting ``task.text``, so the notes travel with every handover.
    """
    if not task.notes:
        return ""
    notes = "; ".join(" ".join(n.split()) for n in task.notes)
    return f". The user later added: {notes}"


def _planning_text(s: store.Store, task: store.Task, retry: bool = False) -> str:
    """The handover that asks Claude to plan ``task``.

    ``retry`` (a planning Stop found no plan file) changes the wording, so the
    spin guard, which suppresses a byte-identical handover on a continuation,
    lets the one retry through; ``Task.attempts`` still caps retries at two.
    """
    handoffs = [
        str(p) for p in (paths.handoff_path(s.session_id, d) for d in task.depends_on)
        if p.exists()
    ]
    before = ", ".join(handoffs) if handoffs else "none"
    plan = paths.plan_path(s.session_id, task.id)
    ending = (
        "then carry it out immediately without waiting."
        if task.auto else
        "then stop and wait for approval."
    )
    attempt = (
        f"{PREFIX}: second attempt, no plan file was found at {plan} after the last turn. "
        if retry else f"{PREFIX}: "
    )
    return (
        f"{attempt}next task is {task.id}: {task.text}{_notes(task)}. "
        f"Read the handoff of what came before at {before}. "
        f"Write a short plan for {task.id} to {plan} (goal, steps, how to verify), "
        f"present it to the user in a few lines, {ending}"
    )


def _represent_text(s: store.Store, task: store.Task) -> str:
    plan = paths.plan_path(s.session_id, task.id)
    return (
        f"{PREFIX}: task {task.id} is planned at {plan} and waiting for your approval; "
        "present the plan briefly and stop."
    )


def _execution_text(s: store.Store, task: store.Task) -> str:
    """The execution handover.

    An ``auto`` task's planning handover already told Claude to carry the
    plan out at once, so by the time this text arrives the work may be done:
    its wording is idempotent (M3) so a second pass neither redoes nor undoes
    anything. An approved task gets the plain instruction.
    """
    plan = paths.plan_path(s.session_id, task.id)
    if task.auto:
        return (
            f"{PREFIX}: task {task.id} is approved. Execute the plan at {plan} now "
            "if you have not already carried it out; if you have, say so and stop."
        )
    return (
        f"{PREFIX}: task {task.id} is approved. Execute the plan at {plan} now. "
        "When it is done, say what was produced and stop."
    )


def _verify_failed_text(task: store.Task, verdict: done.Verdict) -> str:
    # An empty or whitespace-only tail is omitted, so the handback never
    # reads ``Output tail: .`` (M2 fixes observation).
    output_tail = (verdict.output_tail or "").strip()
    tail = f"Output tail: {output_tail}. " if output_tail else ""
    return (
        f"{PREFIX}: task {task.id} failed its verify command ({verdict.reason}). "
        f"{tail}Fix it, then stop."
    )


def _not_done_text(task: store.Task, verdict: done.Verdict) -> str:
    return f"{PREFIX}: task {task.id} does not look finished: {verdict.reason}. Finish it, then stop."


# --- files -------------------------------------------------------------------


def _read(path: Path, limit: int | None = None) -> str:
    """The file's text (clipped to ``limit``), or ``""`` when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:limit] if limit is not None else text


def _plan_excerpt(s: store.Store, task: store.Task) -> str:
    return _read(paths.plan_path(s.session_id, task.id), PLAN_EXCERPT_CHARS)


def _plan_written(s: store.Store, task: store.Task) -> bool:
    """True when the task's plan file exists and holds something."""
    return bool(_read(paths.plan_path(s.session_id, task.id)).strip())


def write_handoff(
    s: store.Store,
    task: store.Task,
    verdict: done.Verdict,
    closing: str | None = None,
) -> Path:
    """Close ``task`` as ``done`` with ``verdict`` and file its handoff note.

    ``closing`` (the Stop event's ``last_assistant_message``) is recorded on
    the task when given; otherwise the closing already on the task is used
    (the question it asked, when the user then confirmed it done). The file
    is what the next task's planning turn reads. Writing it fails open: a
    file that cannot be written is logged and the task is still done.
    """
    task.status = "done"
    if closing is not None:
        task.closing = closing
    task.verdict = verdict.verdict
    notes = "".join(f"- {n}\n" for n in task.notes) or "(none)\n"
    text = (
        f"# Handoff {task.id}\n\n"
        f"## Task\n{task.text}\n\n"
        f"## Notes\n{notes}\n"
        f"## Verdict\n{verdict.verdict}: {verdict.reason}\n\n"
    )
    if verdict.output_tail:
        text += f"## Verify output tail\n{verdict.output_tail}\n\n"
    text += f"## Closing message\n{task.closing or ''}\n"
    path = paths.handoff_path(s.session_id, task.id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        _record(s.session_id, "handoff_write_failed", task=task.id, path=str(path),
                   error=f"{type(exc).__name__}: {exc}")
    _record(s.session_id, "handoff", task=task.id, verdict=verdict.verdict,
               reason=verdict.reason, path=str(path))
    return path


# --- stage A: close the running task ----------------------------------------


def _close_running(
    s: store.Store,
    event: dict,
    shadow: bool,
    claude: str,
    verdict: done.Verdict | None = None,
) -> tuple[dict | None, bool]:
    """Decide the running task's fate. Returns ``(output, proceed)``.

    ``proceed`` is True when the running slot is free and ``choose_next``
    should run; otherwise ``output`` is the Stop output for this turn (a
    handback, or ``None`` when the store now waits for the user's answer).
    """
    sid = s.session_id
    running = s.get(s.running_task) if s.running_task else None
    if running is None:
        return None, True
    if running.status == "paused":
        # A task the user paused mid-turn (amend note "pause", g27) is set
        # aside, not finished: it keeps its status and nothing that depends
        # on it becomes ready. Only the running slot is freed.
        return None, True

    closing = event.get("last_assistant_message")
    closing = closing if isinstance(closing, str) else ""
    if verdict is None:
        verdict = _verdict_for(s, running, event, claude)

    if shadow:
        # Observe only: the verdict is filed and logged, never acted on
        # (no handback, no wait for an answer).
        _record(sid, "shadow_verdict", task=running.id, verdict=verdict.verdict,
                   reason=verdict.reason, shadow=True)
        write_handoff(s, running, verdict, closing)
        return None, True

    if verdict.verdict in ("verify_failed", "not_done") and running.attempts < MAX_ATTEMPTS:
        running.attempts += 1
        _record(sid, "dispatch", task=running.id, verdict=verdict.verdict,
                   reason=verdict.reason, handback=True, attempts=running.attempts, next=None)
        if verdict.verdict == "verify_failed":
            return handback(_verify_failed_text(running, verdict)), False
        return handback(_not_done_text(running, verdict)), False

    if verdict.verdict == "asked_question":
        # The turn ends; the user's next message is the answer (or a
        # confirmation that the task is done). Intake routes it (Task 6).
        running.closing = closing
        s.state = "awaiting_answer"
        s.pending_task = running.id
        s.running_task = None
        _record(sid, "dispatch", task=running.id, verdict="asked_question",
                   reason=verdict.reason, next=None, waiting="awaiting_answer")
        return None, False

    if verdict.verdict == "verify_failed":
        # The verify command never passed: the task is set aside, not done,
        # so nothing that depends on it becomes ready (plan review focus 3).
        running.status = "failed_verify"
        running.verdict = "verify_failed"
        running.closing = closing
        s.running_task = None
        s.state = "idle"
        _record(sid, "dispatch", task=running.id, verdict="verify_failed", reason=verdict.reason,
                   failed_verify=True, next=None)
        return {"systemMessage": _failed_verify_message(running)}, True
    if verdict.verdict == "not_done":
        # Attempts exhausted: close it rather than nag, and say so in the log
        # and the handoff.
        verdict = done.Verdict(
            "gave_up",
            f"gave up after {running.attempts} handbacks; last not_done: {verdict.reason}",
            verdict.output_tail,
        )
    write_handoff(s, running, verdict, closing)
    return None, True


def _verdict_for(s: store.Store, task: store.Task, event: dict, claude: str) -> done.Verdict:
    """The verdict for ``task``: its verify command, the judge for harness
    tasks, and ``stopped`` for a turn the user typed (no model call)."""
    if task.verify or task.kind != "prompt":
        return done.decide(task, event, _plan_excerpt(s, task), claude=claude)
    return done.Verdict("stopped", "prompt turn ended")


def _precompute_verdict(session_id: str, event: dict, claude: str) -> tuple[str | None, str | None, done.Verdict | None]:
    """Decide the running task's fate before taking the store lock.

    A verify command can run for 120 s and the judge for 15 s; holding the
    lock that long makes intake wait and lets a mid-run message through
    unsorted. Returns ``(task id, verify command, verdict)`` so the locked
    stage can check the store did not change underneath.
    """
    s = store.load(session_id)
    if s.interrupt or s.state != "running" or not s.running_task:
        return None, None, None
    task = s.get(s.running_task)
    if task is None or task.status == "paused":
        return None, None, None
    return task.id, task.verify, _verdict_for(s, task, event, claude)


# --- stage B: after a planning turn -----------------------------------------


def _after_planning(s: store.Store) -> tuple[dict | None, bool]:
    """Record the plan Claude just wrote, or ask again, or give the task back.

    Returns ``(output, proceed)`` like ``_close_running``; ``proceed`` is only
    True when nothing was actually being planned.
    """
    sid = s.session_id
    task = s.get(s.pending_task) if s.pending_task else None
    if task is None:
        _record(sid, "plan_failed", task=s.pending_task, reason="no pending task")
        return None, True
    if task.status != "queued":
        # The user cancelled, parked or dropped the task while Claude was
        # writing its plan (control works in every state). It must not come
        # back as ``planned``, so the plan file is ignored and ``choose_next``
        # moves on.
        _record(sid, "plan_failed", task=task.id, reason=f"pending task is {task.status}")
        return None, True

    if _plan_written(s, task):
        task.status = "planned"
        task.plan_based_on = s.done_ids()
        task.attempts = 0
        if task.auto and s.continuations >= MAX_CONTINUATIONS:
            s.state = "awaiting_approval"
            _record(sid, "dispatch", planned=task.id, auto=True, next=None,
                       continuation_budget=s.continuations)
            return {"systemMessage": _budget_message(s.continuations)}, False
        if task.auto:
            # Review focus 5: no approval stop; plan and execution in one
            # continuation.
            text = start(s, task)
            _record(sid, "dispatch", planned=task.id, auto=True, next=task.id)
            return handback(text), False
        s.state = "awaiting_approval"
        _record(sid, "dispatch", planned=task.id, auto=False, next=None)
        return None, False

    task.attempts += 1
    if task.attempts < MAX_ATTEMPTS:
        _record(sid, "dispatch", next=task.id, plan_retry=task.attempts)
        return handback(_planning_text(s, task, retry=True)), False

    # Review focus 2: Claude stopped twice without a plan (asked something,
    # hit an error). Give the task back to the queue and end the turn; it is
    # planned again when a later Stop finds it ready.
    task.status = "queued"
    task.attempts = 0
    s.state = "idle"
    s.pending_task = None
    _record(sid, "plan_failed", task=task.id, attempts=MAX_ATTEMPTS)
    return None, False


# --- stage E: execution handover --------------------------------------------


def start(s: store.Store, task: store.Task) -> str:
    """Apply the execution transition and return the handover text.

    Called from ``_after_planning`` for an ``auto`` task and from intake when
    the user approves a plan (Task 6); each caller wraps the text in its own
    hook envelope.
    """
    task.status = "running"
    task.attempts = 0
    s.running_task = task.id
    s.state = "running"
    s.pending_task = None
    return _execution_text(s, task)


# --- stage D: what runs next -------------------------------------------------


def _shadow_ready(s: store.Store) -> list[store.Task]:
    """Shadow-queued tasks whose dependencies are done or shadow-dispatched."""
    by_id = {t.id: t for t in s.tasks}
    out = []
    for t in s.tasks:
        if t.status != SHADOW_QUEUED:
            continue
        if all(d in by_id and by_id[d].status in _SATISFIED for d in t.depends_on):
            out.append(t)
    return out


def _planned_ready(s: store.Store) -> list[store.Task]:
    """Planned tasks whose every dependency is done, in creation order."""
    done_now = set(s.done_ids())
    return [
        t for t in s.tasks
        if t.status == "planned" and all(d in done_now for d in t.depends_on)
    ]


def _replan_stale(s: store.Store) -> None:
    """Send a planned task back to ``queued`` when a dependency finished after
    its plan was written; the old plan file goes."""
    for t in s.stale_planned():
        t.status = "queued"
        t.plan_based_on = []
        t.attempts = 0
        with contextlib.suppress(OSError):
            paths.plan_path(s.session_id, t.id).unlink()
        _record(s.session_id, "replan", task=t.id)


def choose_next(s: store.Store, stop_hook_active: bool = False, shadow: bool = False) -> str | None:
    """Stage D: the handover text for what happens next, or ``None``.

    Reached after a task closed or from ``idle``. Intake calls it too, when
    the user confirms a task done (Task 6). While a plan is on screen
    (``awaiting_approval``) nothing is chosen: that plan is already next.
    """
    sid = s.session_id
    if s.state == "awaiting_approval":
        return None

    s.state = "idle"
    s.running_task = None
    s.pending_task = None
    _replan_stale(s)

    if s.paused:
        # `queue: pause`: the running task still closed, but nothing is
        # handed over (real or shadow) until `queue: resume`.
        _record(sid, "dispatch", next=None, shadow=shadow, paused=True)
        return None

    if shadow:
        # Report what would run, mark it so the next Stop reports the one after.
        would = _shadow_ready(s)
        if would:
            would[0].status = SHADOW_DISPATCHED
        _record(sid, "dispatch", next=would[0].id if would else None, shadow=True)
        return None

    planned = _planned_ready(s)
    if planned:
        # Review focus 1: a plan whose approval was deferred by an unrelated
        # turn is shown again, never lost and never run unapproved.
        task = planned[0]
        s.state = "awaiting_approval"
        s.pending_task = task.id
        _record(sid, "dispatch", next=task.id, shadow=False, represent=True)
        return _represent_text(s, task)

    ready = s.ready()
    if not ready:
        _record(sid, "dispatch", next=None, shadow=shadow)
        return None
    nxt = ready[0]

    # The spin guard lives in ``handle`` (``_issue``): it compares the text
    # about to go out with the last one, which is what a spin repeats.
    if s.continuations >= MAX_CONTINUATIONS:
        _record(sid, "dispatch", next=nxt.id, shadow=shadow, continuation_budget=s.continuations)
        return None

    _record(sid, "dispatch", next=nxt.id, shadow=shadow)
    s.state = "planning"
    s.pending_task = nxt.id
    return _planning_text(s, nxt)


# --- housekeeping -------------------------------------------------------------


def _maybe_gc(s: store.Store) -> None:
    """Run session garbage collection once per session (M3 Task 2).

    The first Stop of a session stamps ``gc_at`` and calls ``gc.collect``
    with this session protected; later Stops skip it. The stamp is written
    before the call, so a collector that raises is not retried every turn,
    and every error is swallowed: housekeeping never costs the user a
    handover. A ``gc`` log line is written only when a session or a stale
    pointer was deleted or the run failed; ``gc_at`` already says that it
    ran. The result is only logged: ``gc.collect`` returns
    ``(deleted_sessions, deleted_pointers)`` (M4 Task 3).
    """
    if s.gc_at:
        return
    s.gc_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        deleted, pointers = gc.collect(protect={s.session_id})
    except Exception as exc:
        _record(s.session_id, "gc", error=f"{type(exc).__name__}: {exc}")
        return
    if deleted or pointers:
        _record(s.session_id, "gc", deleted=list(deleted), pointers=list(pointers))


# --- entry point -------------------------------------------------------------


def _issue(s: store.Store, out: dict | None, stop_hook_active: bool) -> dict | None:
    """Record the handover in ``out``, or suppress it when it is a spin.

    ``out`` without an ``additionalContext`` (``None``, or a bare
    ``systemMessage``) passes through untouched. Otherwise its text is hashed:
    on a Stop-hook continuation whose text equals ``Store.last_handback``
    the handover is replaced by ``SPIN_GUARD_MESSAGE`` and logged with
    ``spin_guard=True``; the store keeps the state the stage set (so a
    planning spin stays ``planning`` with its ``pending_task``, which intake
    requeues on the user's next prompt). Any other text becomes the new
    ``last_handback``.
    """
    if not out:
        return out
    text = out.get("hookSpecificOutput", {}).get("additionalContext")
    if not isinstance(text, str) or not text:
        return out
    digest = _digest(text)
    if stop_hook_active and digest == s.last_handback:
        _record(s.session_id, "dispatch", next=s.pending_task or s.running_task, spin_guard=True)
        return {"systemMessage": SPIN_GUARD_MESSAGE}
    s.last_handback = digest
    return out


def handle(event: dict, shadow: bool = False, claude: str = "claude") -> dict | None:
    """Run the stages that apply to the store's state; return the Stop output."""
    hookio.start_clock(event)
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    active = bool(event.get("stop_hook_active"))
    # The verify command or judge runs before the lock is taken (review
    # should-fix: a 120 s verify held the session lock and made intake wait).
    pre_id, pre_verify, pre_verdict = _precompute_verdict(session_id, event, claude)
    with store.locked(session_id) as s:
        _maybe_gc(s)
        s.continuations = s.continuations + 1 if active else 0
        if s.interrupt:
            # The user asked to stop but no tool call came after the flag was
            # set; the turn is ending now, so apply the same reset and dispatch
            # nothing.
            task_id, _dropped = reset_after_interrupt(s)
            _record(session_id, "interrupt", task=task_id, via=event.get("hook_event_name"))
            return None

        extra: dict | None = None
        if s.state == "planning":
            out, proceed = _after_planning(s)
            if not proceed:
                return _issue(s, out, active)
        elif s.state in _WAITING:
            # The user's turn ran something else, or nothing changed; the
            # pending plan or question is still waiting on the user.
            _record(session_id, "dispatch", next=None, waiting=s.state)
            return None
        elif s.state == "running" or s.running_task:
            running = s.get(s.running_task) if s.running_task else None
            same = running is not None and running.id == pre_id and running.verify == pre_verify
            out, proceed = _close_running(s, event, shadow, claude, verdict=pre_verdict if same else None)
            if not proceed:
                return out
            extra = out  # a systemMessage to carry (failed_verify), or None

        text = choose_next(s, stop_hook_active=active, shadow=shadow)
        if text:
            return _issue(s, handback(text, (extra or {}).get("systemMessage")), active)
        if s.continuations >= MAX_CONTINUATIONS and s.ready():
            return {"systemMessage": _budget_message(s.continuations)}
        return extra or None
