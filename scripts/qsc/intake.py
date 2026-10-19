"""Intake logic for the ``UserPromptSubmit`` hook. Stdlib only.

``handle(event)`` sorts one user message: rules first (``qsc.rules``), the
``claude -p`` classifier (``qsc.classify``) only for what the rules cannot
decide. M1 adds four branches on top of the M0 steer / queue / interrupt
routing, in this order:

* ``control``: a ``queue: ...`` or ``/queue ...`` prompt runs one command
  from ``qsc.control`` against the store and is blocked with the reply. It
  works in every state, never reaches the classifier, never counts as a
  turn, and still acts in shadow mode (it is the user's explicit command).
* ``dedupe``: a message whose normalised task text equals a task created in
  the last ten minutes is blocked with a pointer to that task. Exact after
  normalisation only (docs/m0-acceptance.md issue 1, second half: held text
  returns to the input box and gets resubmitted).
* ``parked``: a maybe-later item (rule marker or the model's ``parked`` flag)
  is stored with status ``parked`` and never dispatched until promoted.
* ``amend`` and ``split`` from the v2 decision contract: an amend cancels,
  pauses, rewires or annotates a live task; a split applies its queue and
  amend parts and lets the steer parts through with an ``additionalContext``
  naming what was handled.

It also keeps the per-session store in step with the turn: a message whose
``prompt_id`` differs from the one being run starts a new turn, and a new
turn arriving while the store still says ``running`` means the previous turn
was interrupted by Esc (D5): the running task is marked ``interrupted``,
queued tasks are ``dropped``, and a notice listing them is attached to the
new prompt. A new turn arriving while the store says ``planning`` means Esc
cut the planning turn instead (M3): the task being planned goes back to
``queued``, its half-written plan file is deleted, nothing is dropped (the
user interrupted planning, not work), and the notice says it is queued
again; dispatch plans it afresh after this turn.

At ``idle`` (nothing running, nothing pending) the rule markers mean
something else than mid-turn (M3, ``_idle_turn``): ``later: x`` has nothing
to wait for, so ``x`` (the remainder, not the raw prompt) becomes the turn
that starts now and the prompt passes through with a context line saying
so; a tentative marker parks its item and blocks, nothing runs; and "stop"
is answered with ``nothing is running``, no task and no interrupt flag.
Dedupe still runs first, so held marker text sent again at idle is caught.

In shadow mode with the ``shadow_compare`` option on, a message the rules
decided is also sent to the classifier; the log then carries ``rule_label``
and ``classifier`` side by side, and the acted label stays the rule's. Every
decision line also logs ``latency_ms``, the wall-clock of the classifier call.

M2 adds the two waiting states of the plan flow (spec sections 3 and 7,
decisions D3 and D4). The store state is read **before** the rules' ``pass``
short-circuit, so "ok" approves a plan that is waiting but is still a
non-task when nothing waits (review focus 4). Control messages still win
first in every state. ``_waiting`` then sorts the message:

* ``awaiting_approval`` (a plan for ``pending_task`` is on screen): an
  approval word (``rules.is_approval``) or the classifier's ``steer`` with
  ``note: approve`` runs the plan (``dispatch.start``, the execution handover
  rides on the message as ``additionalContext``); ``note: revise plan`` lets
  the message through with a revise instruction and keeps waiting; a
  ``split`` applies its queue and amend parts and then approves or revises
  (g46); a ``later:`` marker queues after the pending task; "stop" is
  answered with a pointer, nothing is running. Anything else is a new turn:
  the plan stays ``planned`` and dispatch re-presents it after that turn
  (review focus 1).
* ``awaiting_answer`` (the running task asked the user something): a
  confirmation (``rules.is_confirmation``: "ok", "ok, merge") closes the
  task as ``confirmed`` through ``dispatch.write_handoff`` and hands the next
  task's planning over on the same message; a queue marker queues; anything
  else is the answer, the task resumes ``running`` and the message passes
  through untouched, since Claude asked the question.

Every branch fails open: anything unresolved is ``steer`` and the message
passes through untouched. The return value is the hook's JSON output, or
``None`` for pass-through.

M4: a pass-through whose ``additionalContext`` tells Claude something the
user should also know (an idle ``later:`` that starts now, an Esc that
dropped or requeued tasks, a split's queued parts, an approval, a revise, a
confirmation, a pause) carries a one-line ``systemMessage`` next to it,
since the UI shows ``systemMessage`` but not ``additionalContext``
(docs/m3-acceptance.md, observation 1). The context is never changed by it.
An answer to the running task's question says nothing: there is nothing to
say.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from . import classify, control, dispatch, done, hookio, log, match, paths, rules, store, transcript

HOOK_EVENT = "UserPromptSubmit"
PREFIX = "queue-switchclaude"

# The model's answer counts only at or above this confidence (spec §10 M0),
# for every actionable label: queue, amend, split, interrupt, parked queue.
CONFIDENCE_THRESHOLD = 0.9
ACTIONABLE_LABELS = ("queue", "amend", "split")

# A task created with the same normalised text inside this window is a
# resubmission of held text, not a new request.
DEDUPE_WINDOW_SECONDS = 600
# Statuses a duplicate is reported against. Dropped and cancelled tasks are
# excluded by Store.find_recent itself; interrupted tasks are gone too.
DEDUPE_STATUSES = ("queued", "running", "parked", "paused", "shadow_queued")
# A finished task only counts as a duplicate when the resend carries a queue
# marker: "later: x" sent again after x ran is the held text coming back
# (docs/m0-acceptance.md issue 1), but a plain "run the tests" typed twice is
# the user asking for it again.
DEDUPE_FINISHED_STATUSES = ("done", "shadow_dispatched")
DEDUPE_MARKER_RULES = ("queue_marker", "parked_marker")

Classifier = Callable[[dict], Optional[classify.Decision]]

# Store states in which the user's message is sorted by ``_waiting`` first.
WAITING_STATES = ("awaiting_approval", "awaiting_answer")
# States in which a message carrying the turn's own ``prompt_id`` is
# mid-turn: a task is executing, or Claude is writing a plan in a Stop-hook
# continuation of the same turn (dispatch keeps ``current_prompt_id``).
MID_TURN_STATES = ("running", "planning")

SHADOW_QUEUED = "shadow_queued"

# Returned by a branch that had nothing to act on, so the caller carries on;
# distinct from ``None``, which is a pass-through the caller returns as is.
_UNHANDLED = object()


# The event ``handle`` is working on, so ``_record`` can hand it to
# ``hookio.hook_ms``; None between calls (``hook_ms`` then reads 0).
_event: dict | None = None


def _record(session_id: str, event_name: str, **fields) -> None:
    """``log.record`` plus ``hook_ms``, the time since the hook script started."""
    log.record(session_id, event_name, hook_ms=hookio.hook_ms(_event or {}), **fields)


def _block(reason: str) -> dict:
    """A UserPromptSubmit block the user can actually see.

    On Claude Code 2.1.302 the ``reason`` of a block is not displayed; the
    prompt text is echoed as "Original prompt: ..." and returned to the input
    box (docs/m0-acceptance.md, issue 1). ``systemMessage`` is the documented
    "warning message shown to the user" on every event, and
    ``suppressOriginalPrompt`` keeps the held text out of the block message.
    """
    return {
        "decision": "block",
        "reason": reason,
        "systemMessage": reason,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "suppressOriginalPrompt": True,
        },
    }


def _context(text: str, show: str | None = None) -> dict:
    """Pass the prompt through with ``additionalContext`` attached.

    ``show`` is a one-line ``systemMessage`` for the user (M4): the context
    reaches the model but never the screen (docs/m3-acceptance.md,
    observation 1), so a pass-through that changed the queue, or started
    something the user did not ask to start now, says so next to the
    context. The context itself is never altered by it (review focus 1).
    """
    out = {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": text}}
    if show is not None:
        out["systemMessage"] = show
    return out


def _show(*parts: str | None) -> str | None:
    """One ``systemMessage`` line from the parts that are not empty, or None."""
    kept = [p for p in parts if p]
    if not kept:
        return None
    return f"{PREFIX}: " + "; ".join(kept)


class Notice(NamedTuple):
    """What an Esc reset tells the model (``context``, the full line) and the
    user (``show``, the short form, without the prefix)."""

    context: str
    show: str


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a sibling temp file and ``os.replace``,
    so a concurrent reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_pointer(session_id: str, cwd: object = None) -> None:
    """Record the session id for ``queue_cli.py``: at ``paths.pointer_path()``
    (the newest session anywhere) and, when the event carries a ``cwd``, at
    ``paths.cwd_pointer_path(cwd)`` (the newest session in that folder).

    Best effort on every event: the ``/queue`` command runs as a Bash call
    with no hook payload, so these files are how it finds the store. The
    Bash tool runs in the project directory, so the cwd pointer is what
    keeps two sessions in different folders apart (M3 review focus 1). Each
    write is atomic and any failure is swallowed; intake never fails over it.
    """
    try:
        targets = [paths.pointer_path()]
        if isinstance(cwd, str) and cwd:
            targets.append(paths.cwd_pointer_path(cwd))
    except Exception:
        return
    for path in targets:
        try:
            _write_atomic(path, session_id + "\n")
        except Exception:
            pass


def _esc_notice(dropped: list[store.Task]) -> Notice | None:
    """The notice for an Esc-interrupted turn; None when nothing was queued."""
    if not dropped:
        return None
    listed = ", ".join(f"{t.id} ({t.text})" for t in dropped)
    return Notice(
        f"{PREFIX}: the previous turn was interrupted, dropped queued tasks: {listed}",
        "Esc dropped " + ", ".join(t.id for t in dropped),
    )


def _planning_notice(task_id: str) -> Notice:
    """The notice for an Esc that cut a planning turn (M3)."""
    return Notice(
        f"{PREFIX}: the planning turn for {task_id} was interrupted; it is queued again",
        f"{task_id} is queued again",
    )


def _interrupt_planning(s: store.Store) -> str | None:
    """Esc during planning: give the task being planned back to the queue.

    Returns the id of the task requeued, or ``None`` when nothing was really
    being planned (no pending task, or one the user cancelled, parked or
    dropped meanwhile: control works in every state, and ``_after_planning``
    makes the same exception, so a cancelled task is never resurrected).
    The half-written plan file goes with it; dispatch asks for a fresh plan
    when a later Stop finds the task ready again.
    """
    task = s.get(s.pending_task) if s.pending_task else None
    if task is None or task.status != "queued":
        return None
    task.status = "queued"
    task.attempts = 0
    with contextlib.suppress(OSError):
        paths.plan_path(s.session_id, task.id).unlink()
    return task.id


def _new_turn(
    s: store.Store,
    event: dict,
    prompt: str,
    prompt_id: str | None,
    *,
    text: str | None = None,
    marker_stripped: bool = False,
    notice: Notice | None = None,
    shadow: bool = False,
) -> dict | None:
    """Register a fresh turn: handle a missed Esc, then make the prompt the running task.

    ``text`` is the running task's text when it differs from the prompt (an
    idle ``later: x`` runs as ``x``); ``marker_stripped`` says so in the log.
    An Esc notice rides on the prompt as context and, outside shadow mode,
    as a ``systemMessage`` (in shadow the dropped tasks were never shown as
    queued, so nothing on screen changes).
    """
    interrupted = None
    interrupted_planning = None
    dropped: list[store.Task] = []
    if s.state in ("running", "planning"):
        notice, interrupted, dropped, interrupted_planning = _reset_turn_details(s)

    s.state = "running"
    s.current_prompt_id = prompt_id
    s.interrupt = False
    # A planned task whose approval this turn defers keeps its status;
    # dispatch finds it again through ``_planned_ready`` after the turn.
    s.pending_task = None
    t = s.new_task(prompt if text is None else text, [], prompt_id, kind="prompt")
    t.status = "running"
    s.running_task = t.id

    _record(
        s.session_id, "intake",
        decision="new_turn",
        task=t.id,
        prompt_id=prompt_id,
        interrupted=interrupted,
        dropped=[d.id for d in dropped],
        interrupted_planning=interrupted_planning,
        marker_stripped=marker_stripped,
    )
    if notice is None:
        return None
    return _context(notice.context, show=None if shadow else _show(notice.show))


def _reset_turn_details(s: store.Store) -> tuple[Notice | None, str | None, list[store.Task], str | None]:
    """A new prompt arrived while the store said running or planning: the
    user pressed Esc. Returns ``(notice, interrupted id, dropped tasks,
    interrupted planning id)`` and leaves the store idle.

    Running: no Stop fired for the task that was running, so the turn was
    cut by Esc (D5): the task is interrupted and queued tasks are dropped.
    Planning: the user interrupted planning, not work, so D5's drop does not
    apply; the task being planned rejoins the queue.
    """
    notice = None
    interrupted = None
    interrupted_planning = None
    dropped: list[store.Task] = []
    if s.state == "running":
        running = s.get(s.running_task) if s.running_task else None
        if running is not None and running.status == "running":
            running.status = "interrupted"
            interrupted = running.id
        dropped = [t for t in s.tasks if t.status in ("queued", SHADOW_QUEUED)]
        for t in dropped:
            t.status = "dropped"
        notice = _esc_notice(dropped)
    elif s.state == "planning":
        interrupted_planning = _interrupt_planning(s)
        if interrupted_planning is not None:
            notice = _planning_notice(interrupted_planning)
    s.state = "idle"
    s.running_task = None
    s.pending_task = None
    s.interrupt = False
    return notice, interrupted, dropped, interrupted_planning


def _reset_turn(s: store.Store) -> Notice | None:
    """The Esc reset alone, for a marker typed right after Esc; logs it."""
    notice, interrupted, dropped, interrupted_planning = _reset_turn_details(s)
    _record(s.session_id, "intake", decision="esc_reset", interrupted=interrupted,
            dropped=[d.id for d in dropped], interrupted_planning=interrupted_planning)
    return notice


def _classifier_payload(s: store.Store, event: dict, text: str) -> dict:
    # While a plan or a question waits, the pending task is what the message
    # is about (the prompt's context E shows it as ``running_task``).
    current = s.running_task or s.pending_task
    running = s.get(current) if current else None
    running_dict = {"id": running.id, "text": running.text} if running else None
    queue = [
        {"id": t.id, "text": t.text, "status": t.status, "depends_on": list(t.depends_on)}
        for t in s.tasks if t.status in ("queued", "parked", "paused")
    ]
    recent = transcript.recent(event.get("transcript_path"))
    return classify.payload(
        s.state, running_dict, queue, recent, text,
        candidates=match.candidates(text, s.candidates()),
    )


def _timed(classifier: Classifier, payload: dict, swallow: bool = False) -> tuple[classify.Decision | None, int]:
    """Call the classifier and measure it: ``(decision, wall-clock ms)``.

    With ``swallow`` an exception from the classifier counts as no answer, for
    the shadow comparison, which must never change what intake does.
    """
    started = time.monotonic()
    try:
        decision = classifier(payload)
    except Exception:
        if not swallow:
            raise
        decision = None
    return decision, int(round((time.monotonic() - started) * 1000))


def _chain_deps(s: store.Store) -> list[str]:
    """M0 dependency rule: the last queued task, else the running task, else nothing.

    Parked and paused tasks are skipped: chaining after a task that may never
    run would hold the queue hostage.
    """
    queued = [t for t in s.tasks if t.status in ("queued", SHADOW_QUEUED)]
    if queued:
        return [queued[-1].id]
    if s.running_task:
        return [s.running_task]
    if s.pending_task:
        # Being planned, waiting for approval, or waiting for an answer: it
        # runs before anything queued now.
        return [s.pending_task]
    return []


def _find_duplicate(s: store.Store, v: rules.Verdict, mid_turn: bool) -> store.Task | None:
    """The live task this prompt resubmits, or None.

    An interrupt phrase is never a duplicate: "stop" must stop even when a
    task called "stop" exists. On a new turn, the task the store still calls
    ``running`` is about to be marked interrupted (Esc, D5), so resending its
    text is a retry, not a duplicate.
    """
    if v.label == "interrupt":
        return None
    dup = s.find_recent(store.normalise(v.text), DEDUPE_WINDOW_SECONDS)
    if dup is None:
        return None
    if dup.status in DEDUPE_FINISHED_STATUSES:
        if v.rule not in DEDUPE_MARKER_RULES:
            return None
    elif dup.status not in DEDUPE_STATUSES:
        return None
    if not mid_turn and dup.status == "running" and dup.id == s.running_task:
        return None
    return dup


def _confident(obj: classify.Decision | classify.Part) -> bool:
    return obj.confidence >= CONFIDENCE_THRESHOLD


def _parts_for_log(decision: classify.Decision | None) -> list[str]:
    return [p.label for p in decision.parts] if decision is not None else []


# --- actions ----------------------------------------------------------------
# Each returns a confirmation string, or None when the branch resolves to
# steer. They never block themselves; the caller decides how to answer.


def _queue(
    s: store.Store,
    text: str,
    depends_on: list[str],
    prompt_id: str | None,
    parked: bool,
    shadow: bool,
) -> str:
    deps = list(depends_on) if depends_on else _chain_deps(s)
    status = SHADOW_QUEUED if shadow else ("parked" if parked else "queued")
    t = s.new_task(text, deps, prompt_id, status=status)
    if parked:
        return f"parked {t.id}: {t.text} (send 'queue: promote {t.id}' to run it)"
    after = ", ".join(deps) if deps else "nothing"
    return f"queued {t.id} after {after}: {t.text}"


def _resolve_target(s: store.Store, text: str, named: str | None) -> tuple[store.Task | None, bool]:
    """The task an amend is about, and whether the model named it.

    ``named`` wins when it is a live candidate; otherwise ``match.pick`` must
    find one clear subject, or the amend is unresolved (``None``).
    """
    live = s.candidates()
    if named:
        for t in live:
            if t.id == named:
                return t, True
    return match.pick(text, live), False


def _amend(
    s: store.Store,
    text: str,
    decision: classify.Decision | classify.Part,
    shadow: bool,
) -> tuple[str | None, str | None]:
    """Apply one amend. Returns ``(confirmation, target_id)``; confirmation
    is None when the amend falls through to steer.

    Precedence: cancel, pause, dependency rewire, note. A dependency rewire
    is applied only when the model named the target itself: when the target
    came from text matching, the model's ``depends_on`` was written without
    knowing which task it was about and cannot be a rewiring instruction.
    """
    target, named = _resolve_target(s, text, decision.target)
    if target is None:
        return None, None
    if shadow:
        return f"would amend {target.id}", target.id

    if decision.note == "cancel":
        cancelled = s.cancel(target.id)
        if cancelled is None:
            # Running (or done): not a queue edit; let Claude handle the message.
            return None, target.id
        return f"cancelled {cancelled.id}: {cancelled.text}", target.id

    if decision.note == "pause":
        # Only meaningful on the running task: the turn keeps going, and the
        # next Stop leaves it paused instead of done (dispatch, Task 7).
        target.status = "paused"
        return f"paused {target.id}", target.id

    if named and list(decision.depends_on) != list(target.depends_on):
        target.depends_on = list(decision.depends_on)
        ids = ", ".join(target.depends_on) if target.depends_on else "nothing"
        return f"{target.id} now depends on {ids}", target.id

    target.notes.append(text)
    return f"noted on {target.id}: {text}", target.id


def _pause(s: store.Store, named: str | None) -> tuple[str | None, str | None]:
    """Apply a steer's ``note: "pause"``: the named live task, else the running one.

    The golden set's g27 ("actually do the webhook first, refactor can wait")
    is a steer part with note pause and an amend part that rewires. Dispatch
    leaves a paused running task paused instead of marking it done, and
    ``queue: resume pN`` or ``promote pN`` brings it back.
    """
    target = None
    if named:
        for t in s.candidates():
            if t.id == named:
                target = t
                break
    if target is None and s.running_task:
        target = s.get(s.running_task)
    if target is None or target.status not in ("running", "queued"):
        return None, None
    target.status = "paused"
    return f"paused {target.id} (send 'queue: resume {target.id}' to run it again)", target.id


def _clean_parts(parts: list[classify.Part]) -> list[classify.Part]:
    """Fake-split guard: drop empty parts, merge parts with identical normalised text."""
    seen: set[str] = set()
    out = []
    for p in parts:
        key = store.normalise(p.text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _id_number(task_id: str) -> int:
    try:
        return int(str(task_id).lstrip("p"))
    except ValueError:
        return 0


def _next_visible_id(s: store.Store) -> int:
    """The number of the first id the model will assign to a new split part.

    The prompt tells the model that new tasks are numbered after the highest
    id it can see, and the payload shows it only the live tasks
    (``s.candidates()``). The store may hold newer ids it was never shown
    (cancelled, dropped, done, interrupted), so the model's prediction and
    ``s.next_id`` can differ; ``_split`` maps one onto the other.
    """
    return max([_id_number(t.id) for t in s.candidates()] + [0]) + 1


def _part_label(p: classify.Part) -> str:
    """The label a split part is acted on as; anything unsure is steer."""
    if not _confident(p) or p.label not in ("queue", "amend"):
        return "steer"
    return p.label


# --- entry point --------------------------------------------------------------


def handle(event: dict, classifier: Classifier = classify.run, shadow: bool = False) -> dict | None:
    """Sort one UserPromptSubmit event. Returns the hook output or None to pass through."""
    global _event
    _event = event
    hookio.start_clock(event)
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    prompt_id = event.get("prompt_id")
    _write_pointer(session_id, event.get("cwd"))

    v = rules.decide(prompt)
    if v.label == "pass" and not _pass_may_wait(v, prompt, session_id):
        return None

    with store.locked(session_id) as s:
        if v.label == "control":
            return _control(s, v, prompt, prompt_id, shadow)
        if s.state in WAITING_STATES:
            return _waiting(s, event, v, prompt, prompt_id, classifier, shadow)
        return _turn(s, event, v, prompt, prompt_id, classifier, shadow)


def _pass_may_wait(v: rules.Verdict, prompt: str, session_id: str) -> bool:
    """Whether a ``pass`` verdict still needs the store: a non-task word
    ("ok", "yes") while a plan or a question waits on the user (review
    focus 4).

    Harness traffic and slash commands never do: a system reminder is not
    an answer and "/compact" is not an approval. The unlocked peek is one
    file read and writes nothing, so an idle "ok" leaves no store behind.
    """
    if v.rule != "non_task" or prompt.strip().startswith("/"):
        return False
    return store.load(session_id).state in WAITING_STATES


def _control(s: store.Store, v: rules.Verdict, prompt: str, prompt_id: str | None, shadow: bool) -> dict:
    """Run one ``queue:`` command and block with its reply.

    ``queue: approve`` is the exception: control moved the pending task to
    ``running`` (Task 3), but a block starts no turn, so the execution
    handover rides on this prompt as ``additionalContext``, exactly as
    "lgtm" does.
    """
    before_state, before_pending = s.state, s.pending_task
    reply = control.run(s, v.control or "", session_id=s.session_id)
    approved = (
        before_state == "awaiting_approval"
        and s.state == "running"
        and s.running_task is not None
        and s.running_task == before_pending
    )
    _record(
        s.session_id, "intake",
        decision="control",
        message=prompt,
        rule=v.rule,
        label="control",
        command=v.control,
        approved=s.running_task if approved else None,
        shadow=shadow,
        prompt_id=prompt_id,
    )
    if approved:
        task = s.get(s.running_task)
        assert task is not None
        text = dispatch.start(s, task)
        s.current_prompt_id = prompt_id
        s.interrupt = False
        return _context(f"{PREFIX}: {reply}. {text}", show=_show(f"running {task.id}"))
    return _block(f"{PREFIX}: {reply}")


def _turn(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    prompt: str,
    prompt_id: str | None,
    classifier: Classifier,
    shadow: bool,
) -> dict | None:
    """The M0/M1 path once the store is locked and nothing waits on the user."""
    if v.label == "pass":
        # The store moved on between the unlocked peek and the lock.
        return None

    mid_turn = s.state in MID_TURN_STATES and prompt_id == s.current_prompt_id

    dup = _find_duplicate(s, v, mid_turn)
    if dup is not None:
        _record(
            s.session_id, "intake",
            decision="dedupe",
            task=dup.id,
            message=prompt,
            rule=v.rule,
            label="dedupe",
            shadow=shadow,
            prompt_id=prompt_id,
        )
        if not shadow:
            return _block(_dedupe_text(dup))
        if mid_turn:
            return None

    if not mid_turn:
        notice = None
        if s.state in ("running", "planning") and _is_marker(v):
            # Esc, then a marker or "stop": apply the Esc reset first so the
            # idle rules below see the store as it really is (review follow-up).
            notice = _reset_turn(s)
        if _idle(s):
            out = _idle_turn(s, event, v, prompt, prompt_id, shadow,
                             run_now=not _waiting_work(s), notice=notice)
            if out is not _UNHANDLED:
                return out
        return _new_turn(s, event, prompt, prompt_id, notice=notice, shadow=shadow)

    return _mid_turn(s, event, v, prompt, prompt_id, classifier, shadow)


def _dedupe_text(dup: store.Task) -> str:
    return f"{PREFIX}: that is already {dup.id} ({dup.status}); send 'queue: list' to see the queue"


# --- M3: idle-state markers ---------------------------------------------------


WAITING_WORK = ("queued", "planned", "paused", "failed_verify")


def _idle(s: store.Store) -> bool:
    """Nothing running, nothing pending: the next message starts a turn."""
    return s.state == "idle" and s.running_task is None and s.pending_task is None


def _waiting_work(s: store.Store) -> bool:
    """Queued, planned, paused or verify-failed tasks exist but nothing runs:
    a paused queue, an exhausted continuation budget, or work blocked behind
    a failed dependency. A ``later: x`` typed now belongs after them."""
    return any(t.status in WAITING_WORK for t in s.tasks)


def _is_marker(v: rules.Verdict) -> bool:
    return v.label == "interrupt" or v.rule in ("queue_marker", "parked_marker")


def _with_notice(notice: Notice | None, text: str) -> str:
    return f"{notice.context}; {text}" if notice else text


def _idle_turn(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
    run_now: bool = True,
    notice: Notice | None = None,
) -> object:
    """A rule verdict at idle, where the markers mean something else than
    mid-turn. Returns the hook output, or ``_UNHANDLED`` for an ordinary
    new turn.

    * ``later: x`` (``queue_marker``): there is nothing to run it after, so
      ``x`` starts now as the running task's text and the prompt passes
      through with a context line saying so, and a ``systemMessage`` so the
      user sees why their "later" started at once (M4).
    * ``maybe later x`` (``parked_marker``): parked and blocked; nothing
      runs until ``queue: promote``.
    * "stop" (``interrupt``): nothing is running, so it is answered and
      blocked; no task is created and no interrupt flag is set, which would
      end the next turn at its first tool call.

    Shadow mode records the decision but changes nothing on screen: the raw
    prompt starts a turn as before, and a tentative item is filed as
    ``shadow_queued`` the way the mid-turn path files it.
    """
    sid = s.session_id

    if v.label == "interrupt":
        _record(sid, "intake", decision="noop", state="idle", message=prompt, rule=v.rule,
                   rule_label=v.label, label="interrupt", shadow=shadow, prompt_id=prompt_id)
        if shadow:
            return _new_turn(s, event, prompt, prompt_id, notice=notice, shadow=True)
        return _block(_with_notice(notice, f"{PREFIX}: nothing is running"))

    if v.label == "queue" and v.parked:
        confirmation = _queue(s, v.text, [], prompt_id, parked=True, shadow=shadow)
        _record(sid, "intake", decision="parked_idle", state="idle", task=s.tasks[-1].id,
                   message=prompt, rule=v.rule, rule_label=v.label, label="queue", parked=True,
                   shadow=shadow, prompt_id=prompt_id)
        if shadow:
            return _new_turn(s, event, prompt, prompt_id, notice=notice, shadow=True)
        return _block(_with_notice(notice, f"{PREFIX}: {confirmation}"))

    if v.label == "queue":
        if shadow:
            return _new_turn(s, event, prompt, prompt_id, notice=notice, shadow=True)
        if not run_now:
            # Work is waiting (paused queue, exhausted budget, a blocked
            # dependency): this belongs after it, not in front of it.
            confirmation = _queue(s, v.text, [], prompt_id, parked=False, shadow=False)
            _record(sid, "intake", decision="queued_idle", state="idle", task=s.tasks[-1].id,
                    message=prompt, rule=v.rule, rule_label=v.label, label="queue",
                    shadow=shadow, prompt_id=prompt_id)
            return _block(_with_notice(
                notice, f"{PREFIX}: {confirmation} (nothing is running; send 'queue: list' to see why)"))
        _new_turn(s, event, prompt, prompt_id, text=v.text, marker_stripped=True)
        return _context(
            _with_notice(notice, f"{PREFIX}: nothing is running, so this starts now: {v.text}"),
            show=_show(notice.show if notice else None, f"nothing was queued, running now: {v.text}"),
        )

    return _UNHANDLED


def _mid_turn(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    prompt: str,
    prompt_id: str | None,
    classifier: Classifier,
    shadow: bool,
) -> dict | None:
    session_id = s.session_id
    decision: classify.Decision | None = None
    compared: classify.Decision | None = None
    latency_ms: int | None = None
    parked = v.parked
    if v.label == "undecided":
        decision, latency_ms = _timed(classifier, _classifier_payload(s, event, v.text))
        label = "steer"
        if decision is not None and _confident(decision):
            if decision.label == "steer" and decision.interrupt:
                label = "interrupt"
            elif decision.label == "steer" and decision.note == "pause":
                label = "pause"
            elif decision.label in ACTIONABLE_LABELS:
                label = decision.label
                parked = decision.label == "queue" and decision.parked
    else:
        label = v.label
        if shadow and hookio.shadow_compare():
            # Shadow comparison: also ask the model about a message the
            # rules already decided, so the log holds both answers. The
            # acted label stays the rule's; ``compared`` is never acted on.
            compared, latency_ms = _timed(classifier, _classifier_payload(s, event, v.text), swallow=True)

    # Resolve the branch before logging so the log carries the acted label
    # (a split that is really a steer logs as steer) and the amend target.
    output: dict | None = None
    target_id: str | None = None

    if label == "interrupt":
        if not shadow:
            s.interrupt = True
            output = _block(f"{PREFIX}: stopping {s.running_task or 'the current turn'} at the next tool call")

    elif label == "pause":
        assert decision is not None
        confirmation, target_id = (None, None) if shadow else _pause(s, decision.target)
        # The message still reaches Claude: it says what to do instead; the
        # user sees that the task was paused.
        output = _context(f"{PREFIX}: {confirmation}", show=_show(confirmation)) if confirmation else None
        label = "pause" if confirmation else "steer"

    elif label == "queue":
        deps = list(decision.depends_on) if decision is not None else []
        confirmation = _queue(s, v.text, deps, prompt_id, parked, shadow)
        if not shadow:
            output = _block(f"{PREFIX}: {confirmation}")

    elif label == "amend":
        assert decision is not None
        confirmation, target_id = _amend(s, v.text, decision, shadow)
        if confirmation is None:
            label = "steer"
        elif not shadow:
            output = _block(f"{PREFIX}: {confirmation}")

    elif label == "split":
        assert decision is not None
        output, label, target_id = _split(s, decision, prompt_id, shadow)

    logged = decision if decision is not None else compared
    _record(
        session_id, "intake",
        message=prompt,
        rule=v.rule,
        rule_label=v.label,
        classifier=dataclasses.asdict(logged) if logged is not None else None,
        label=label,
        latency_ms=latency_ms,
        target=target_id,
        parts=_parts_for_log(decision),
        parked=parked if label == "queue" else False,
        shadow=shadow,
        prompt_id=prompt_id,
    )
    return output


def _apply_parts(
    s: store.Store,
    parts: list[classify.Part],
    labels: list[str],
    prompt_id: str | None,
    shadow: bool,
) -> tuple[list[str], list[str], str | None]:
    """Apply the queue and amend parts of a split; steer parts are collected.

    Returns ``(confirmations, steer_texts, amend_target)``. A later part may
    depend on an earlier part by the id the model predicted for it; those
    predictions are translated to the ids the store really hands out, or the
    dependency points at a task that is not the one meant (and may never
    finish).
    """
    confirmations: list[str] = []
    steer_texts: list[str] = []
    target_id: str | None = None
    predicted = _next_visible_id(s)
    remap: dict[str, str] = {}
    for p, lbl in zip(parts, labels):
        p.depends_on = [remap.get(d, d) for d in p.depends_on]
        if lbl == "queue":
            actual = f"p{s.next_id}"
            confirmations.append(_queue(s, p.text, list(p.depends_on), prompt_id, p.parked, shadow))
            remap[f"p{predicted}"] = actual
            predicted += 1
        elif lbl == "amend":
            confirmation, tid = _amend(s, p.text, p, shadow)
            if confirmation is None:
                steer_texts.append(p.text)
            else:
                confirmations.append(confirmation)
                target_id = target_id or tid
        else:
            steer_texts.append(p.text)
            if p.note == "pause" and not shadow:
                confirmation, tid = _pause(s, p.target)
                if confirmation:
                    confirmations.append(confirmation)
                    target_id = target_id or tid
    return confirmations, steer_texts, target_id


def _split(
    s: store.Store,
    decision: classify.Decision,
    prompt_id: str | None,
    shadow: bool,
) -> tuple[dict | None, str, str | None]:
    """Apply a split decision. Returns ``(output, acted_label, amend_target)``."""
    parts = _clean_parts(decision.parts)
    labels = [_part_label(p) for p in parts]
    if not parts or all(lbl == "steer" for lbl in labels):
        # Nothing to split, or every part is Claude's: a plain steer (review focus 3).
        return None, "steer", None

    confirmations, steer_texts, target_id = _apply_parts(s, parts, labels, prompt_id, shadow)

    if not confirmations:
        return None, "steer", None
    if len(parts) == 1 and labels[0] in ("queue", "amend"):
        # One real part: the decision is that part's label, answered as a standalone.
        acted = labels[0]
        output = None if shadow else _block(f"{PREFIX}: {confirmations[0]}")
        return output, acted, target_id

    handled = "; ".join(confirmations)
    if shadow:
        return None, "split", target_id
    if steer_texts:
        # The user sees the confirmations only; the steer text is their own.
        return _context(
            f"{PREFIX}: handled the rest of this message: {handled}; "
            f"act only on: {' '.join(steer_texts)}",
            show=_show(handled),
        ), "split", target_id
    return _block(f"{PREFIX}: {handled}"), "split", target_id


# --- M2: the waiting states ---------------------------------------------------
# Reached under the lock when the store says ``awaiting_approval`` (a plan for
# ``pending_task`` is on screen) or ``awaiting_answer`` (the running task,
# ``pending_task``, ended its turn by asking the user something). A control
# message never gets here; a ``pass`` verdict does only for a non-task word.


def _log_waiting(
    s: store.Store,
    v: rules.Verdict,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
    *,
    state: str,
    decision: str,
    label: str,
    task: str | None,
    classifier: classify.Decision | None = None,
    latency_ms: int | None = None,
    target: str | None = None,
    **extra,
) -> None:
    _record(
        s.session_id, "intake",
        decision=decision,
        state=state,
        task=task,
        message=prompt,
        rule=v.rule,
        rule_label=v.label,
        classifier=dataclasses.asdict(classifier) if classifier is not None else None,
        label=label,
        latency_ms=latency_ms,
        target=target,
        parts=_parts_for_log(classifier),
        shadow=shadow,
        prompt_id=prompt_id,
        **extra,
    )


def _waiting(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    prompt: str,
    prompt_id: str | None,
    classifier: Classifier,
    shadow: bool,
) -> dict | None:
    """Sort a message that arrived while the store waits on the user."""
    t = s.get(s.pending_task) if s.pending_task else None
    expected = "planned" if s.state == "awaiting_approval" else "running"
    if t is None or t.status != expected:
        # Fail open: the store does not describe a real wait (the planned
        # task was cancelled, the file was edited by hand). Back to idle and
        # the message is an ordinary turn.
        _record(s.session_id, "intake", decision="waiting_reset", state=s.state,
                   task=s.pending_task, prompt_id=prompt_id)
        s.state = "idle"
        s.pending_task = None
        return _turn(s, event, v, prompt, prompt_id, classifier, shadow)
    if s.state == "awaiting_answer":
        return _answer_state(s, event, v, t, prompt, prompt_id, shadow)
    return _approval_state(s, event, v, t, prompt, prompt_id, classifier, shadow)


def _rule_while_waiting(
    s: store.Store,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
) -> tuple[bool, dict | None]:
    """The rule-decided verdicts both waiting states share: a queue marker
    and a stop phrase. Returns ``(handled, output)``.

    "later: x" queues after the pending task (``_chain_deps``) and the store
    keeps waiting. "stop" is answered with a pointer and sets no interrupt
    flag: nothing is running, and a stale flag would end the next turn at
    its first tool call.
    """
    state = s.state
    if v.label == "interrupt":
        if state == "awaiting_approval":
            where = f"waiting for approval (send 'queue: cancel {t.id}' to drop it)"
        else:
            where = "waiting for your answer"
        _log_waiting(s, v, prompt, prompt_id, shadow, state=state, decision="noop", label="interrupt", task=t.id)
        if shadow:
            return True, None
        return True, _block(f"{PREFIX}: nothing is running; {t.id} is {where}")

    if v.label == "queue":
        dup = _find_duplicate(s, v, mid_turn=True)
        if dup is not None:
            _log_waiting(s, v, prompt, prompt_id, shadow, state=state, decision="dedupe", label="dedupe", task=dup.id)
            return True, (None if shadow else _block(_dedupe_text(dup)))
        confirmation = _queue(s, v.text, [], prompt_id, v.parked, shadow)
        _log_waiting(s, v, prompt, prompt_id, shadow, state=state, decision="queue", label="queue",
                     task=t.id, parked=v.parked)
        return True, (None if shadow else _block(f"{PREFIX}: {confirmation}"))

    return False, None


# --- awaiting_approval --------------------------------------------------------


def _handled_suffix(confirmations: list[str], steer_texts: list[str]) -> str:
    """What else a split message carried, appended to a handover text."""
    out = ""
    if confirmations:
        out += " Also handled from this message: " + "; ".join(confirmations) + "."
    if steer_texts:
        out += " The user also said: " + " ".join(steer_texts)
    return out


def _approve(
    s: store.Store,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
    *,
    label: str = "steer",
    classifier: classify.Decision | None = None,
    latency_ms: int | None = None,
    confirmations: list[str] | None = None,
    steer_texts: list[str] | None = None,
) -> dict:
    """Run the waiting plan: the execution transition (``dispatch.start``)
    and its handover text on this message as ``additionalContext``.

    The message itself passes through, so the turn that executes the plan
    is the user's own; its ``prompt_id`` becomes the current one. The user
    sees ``running pN`` (and what else the message queued or amended).
    """
    text = dispatch.start(s, t)
    s.current_prompt_id = prompt_id
    s.interrupt = False
    text += _handled_suffix(confirmations or [], steer_texts or [])
    _log_waiting(s, v, prompt, prompt_id, shadow, state="awaiting_approval", decision="approve",
                 label=label, task=t.id, classifier=classifier, latency_ms=latency_ms,
                 handled=list(confirmations or []))
    return _context(text, show=_show(f"running {t.id}", *(confirmations or [])))


def _revise(
    s: store.Store,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
    *,
    label: str = "steer",
    classifier: classify.Decision | None = None,
    latency_ms: int | None = None,
    confirmations: list[str] | None = None,
) -> dict:
    """"Looks good but X first" (g45): the message reaches Claude with a
    revise instruction; the plan stays ``planned`` and the store keeps
    waiting, so the next Stop changes nothing (dispatch stage C)."""
    plan = paths.plan_path(s.session_id, t.id)
    text = (
        f"{PREFIX}: revise the plan at {plan} according to this message, "
        "present the revised plan, then stop and wait for approval"
    )
    if confirmations:
        text += ". Also handled from this message: " + "; ".join(confirmations)
    s.current_prompt_id = prompt_id
    s.interrupt = False
    _log_waiting(s, v, prompt, prompt_id, shadow, state="awaiting_approval", decision="revise",
                 label=label, task=t.id, classifier=classifier, latency_ms=latency_ms,
                 handled=list(confirmations or []))
    return _context(text, show=_show(f"revising the plan for {t.id}", *(confirmations or [])))


def _defer(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
    *,
    label: str = "steer",
    classifier: classify.Decision | None = None,
    latency_ms: int | None = None,
    context: str | None = None,
    show: str | None = None,
) -> dict | None:
    """An unrelated message while a plan waits: a normal turn (review focus 1).

    The plan keeps its ``planned`` status and only loses the pending slot;
    dispatch finds it through ``_planned_ready`` after this turn and asks
    for approval again. It is never run unapproved and never lost.
    ``context`` (and its on-screen ``show``) name what else a split message
    carried.
    """
    _log_waiting(s, v, prompt, prompt_id, shadow, state="awaiting_approval", decision="defer_approval",
                 label=label, task=t.id, classifier=classifier, latency_ms=latency_ms)
    s.pending_task = None
    out = _new_turn(s, event, prompt, prompt_id, shadow=shadow)
    if context is None:
        return out
    # ``_new_turn`` attaches a notice only after an Esc, which needs state
    # ``running`` or ``planning``; here the state was ``awaiting_approval``,
    # so ``out`` is None.
    return _context(context, show=show)


def _approval_state(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    classifier: Classifier,
    shadow: bool,
) -> dict | None:
    if rules.is_approval(prompt):
        return _approve(s, v, t, prompt, prompt_id, shadow)

    if v.label == "pass":
        # A non-task word while the plan is on screen ("continue", ".") is
        # the user saying go on: treat it as an approval, or Claude would
        # execute the plan while the store still says awaiting_approval.
        return _approve(s, v, t, prompt, prompt_id, shadow)

    handled, output = _rule_while_waiting(s, v, t, prompt, prompt_id, shadow)
    if handled:
        return output

    decision, latency_ms = _timed(classifier, _classifier_payload(s, event, v.text))
    confident = decision is not None and _confident(decision)
    kw = dict(classifier=decision, latency_ms=latency_ms)

    if confident and decision.label == "steer":
        if decision.interrupt:
            _log_waiting(s, v, prompt, prompt_id, shadow, state=s.state, decision="noop", label="interrupt",
                         task=t.id, **kw)
            if shadow:
                return None
            return _block(f"{PREFIX}: nothing is running; {t.id} is waiting for approval "
                          f"(send 'queue: cancel {t.id}' to drop it)")
        if decision.note == "approve":
            return _approve(s, v, t, prompt, prompt_id, shadow, **kw)
        if decision.note == "revise plan":
            return _revise(s, v, t, prompt, prompt_id, shadow, **kw)

    elif confident and decision.label == "queue":
        confirmation = _queue(s, v.text, list(decision.depends_on), prompt_id, decision.parked, shadow)
        _log_waiting(s, v, prompt, prompt_id, shadow, state=s.state, decision="queue", label="queue",
                     task=t.id, parked=decision.parked, **kw)
        return None if shadow else _block(f"{PREFIX}: {confirmation}")

    elif confident and decision.label == "amend":
        confirmation, target_id = _amend(s, v.text, decision, shadow)
        if confirmation is not None:
            _log_waiting(s, v, prompt, prompt_id, shadow, state=s.state, decision="amend", label="amend",
                         task=t.id, target=target_id, **kw)
            return None if shadow else _block(f"{PREFIX}: {confirmation}")

    elif confident and decision.label == "split":
        out = _split_while_waiting(s, event, v, t, prompt, prompt_id, shadow, decision, latency_ms)
        if out is not _UNHANDLED:
            return out

    return _defer(s, event, v, t, prompt, prompt_id, shadow, **kw)


def _split_while_waiting(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
    decision: classify.Decision,
    latency_ms: int | None,
) -> object:
    """A split while a plan waits: "go, and once it's in add logout" (g46).

    The queue and amend parts are applied as in ``_split``; a confident
    steer part with ``note: approve`` then runs the plan and one with
    ``note: revise plan`` revises it, both naming what else was handled.
    Without either: queue-only parts block with their confirmations and the
    store keeps waiting; leftover steer text makes the message a deferred
    turn that still names what was handled. ``_UNHANDLED`` when the split
    had nothing to act on.
    """
    parts = _clean_parts(decision.parts)
    gate = [p for p in parts if p.label == "steer" and p.note in ("approve", "revise plan") and _confident(p)]
    rest = [p for p in parts if p not in gate]
    labels = [_part_label(p) for p in rest]
    confirmations, steer_texts, _target = _apply_parts(s, rest, labels, prompt_id, shadow)
    kw = dict(label="split", classifier=decision, latency_ms=latency_ms)

    if any(p.note == "approve" for p in gate):
        return _approve(s, v, t, prompt, prompt_id, shadow, confirmations=confirmations,
                        steer_texts=steer_texts, **kw)
    if gate:
        return _revise(s, v, t, prompt, prompt_id, shadow, confirmations=confirmations, **kw)
    if not confirmations:
        return _UNHANDLED
    _log_waiting(s, v, prompt, prompt_id, shadow, state=s.state, decision="split", label="split",
                 task=t.id, classifier=decision, latency_ms=latency_ms, handled=confirmations)
    handled = "; ".join(confirmations)
    if steer_texts:
        if shadow:
            return None
        return _defer(s, event, v, t, prompt, prompt_id, shadow, **kw,
                      context=f"{PREFIX}: handled the rest of this message: {handled}; "
                              f"act only on: {' '.join(steer_texts)}",
                      show=_show(handled))
    return None if shadow else _block(f"{PREFIX}: {handled}")


# --- awaiting_answer ----------------------------------------------------------


def _confirm(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
) -> dict:
    """"ok, merge" (spec section 7): the task that asked is done.

    ``dispatch.write_handoff`` files it as ``confirmed`` (its closing message
    is the question it asked, recorded by dispatch), then the next task is
    chosen at once through ``dispatch.choose_next``: dispatch only runs on
    Stop, and this message is the turn. The message passes through with the
    confirmation and the planning (or re-present) handover as context, so
    Claude both acts on it and plans what is next. With nothing to plan, the
    message starts an ordinary turn that carries the confirmation. Either
    way the user sees ``pN confirmed done``.
    """
    dispatch.write_handoff(s, t, done.Verdict("confirmed", "user confirmed"))
    s.state = "idle"
    s.pending_task = None
    text = dispatch.choose_next(s, shadow=shadow)
    notice = f"{PREFIX}: {t.id} confirmed done"
    _log_waiting(s, v, prompt, prompt_id, shadow, state="awaiting_answer", decision="confirm",
                 label="steer", task=t.id, next=s.pending_task if text else None)
    if text:
        s.current_prompt_id = prompt_id
        s.interrupt = False
        return _context(f"{notice}. {text}", show=notice)
    _new_turn(s, event, prompt, prompt_id, shadow=shadow)
    return _context(notice, show=notice)


def _answer_state(
    s: store.Store,
    event: dict,
    v: rules.Verdict,
    t: store.Task,
    prompt: str,
    prompt_id: str | None,
    shadow: bool,
) -> dict | None:
    if rules.is_confirmation(prompt):
        return _confirm(s, event, v, t, prompt, prompt_id, shadow)

    handled, output = _rule_while_waiting(s, v, t, prompt, prompt_id, shadow)
    if handled:
        return output

    # The answer. Claude asked, so the message goes through untouched and
    # the task resumes under this prompt; no classifier call.
    _log_waiting(s, v, prompt, prompt_id, shadow, state="awaiting_answer", decision="answer",
                 label="steer", task=t.id)
    s.state = "running"
    s.running_task = t.id
    s.current_prompt_id = prompt_id
    s.pending_task = None
    s.interrupt = False
    return None
