"""Intake logic for the ``UserPromptSubmit`` hook. Stdlib only.

``handle(event)`` sorts one user message into steer / queue / interrupt:
rules first (``qsc.rules``), the ``claude -p`` classifier (``qsc.classify``)
only for what the rules cannot decide. It also keeps the per-session store
in step with the turn: a message whose ``prompt_id`` differs from the one
being run starts a new turn, and a new turn arriving while the store still
says ``running`` means the previous turn was interrupted by Esc (D5): the
running task is marked ``interrupted``, queued tasks are ``dropped``, and a
notice listing them is attached to the new prompt.

In shadow mode with the ``shadow_compare`` option on, a message the rules
decided is also sent to the classifier; the log then carries ``rule_label``
and ``classifier`` side by side, and the acted label stays the rule's. Every
decision line also logs ``latency_ms``, the wall-clock of the classifier call.

The return value is the hook's JSON output, or ``None`` for pass-through.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Callable, Optional

from . import classify, hookio, log, rules, store, transcript

HOOK_EVENT = "UserPromptSubmit"
PREFIX = "queue-switchclaude"

# The model's answer counts only at or above this confidence (spec §10 M0).
CONFIDENCE_THRESHOLD = 0.9
ACTIONABLE_LABELS = ("queue", "interrupt")

Classifier = Callable[[dict], Optional[classify.Decision]]


SHADOW_QUEUED = "shadow_queued"


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


def _esc_notice(dropped: list[store.Task]) -> str | None:
    """The additionalContext line for an Esc-interrupted turn; None when nothing was queued."""
    if not dropped:
        return None
    listed = ", ".join(f"{t.id} ({t.text})" for t in dropped)
    return f"{PREFIX}: the previous turn was interrupted, dropped queued tasks: {listed}"


def _new_turn(s: store.Store, event: dict, prompt: str, prompt_id: str | None) -> dict | None:
    """Register a fresh turn: handle a missed Esc, then make the prompt the running task."""
    notice = None
    interrupted = None
    dropped: list[store.Task] = []
    if s.state == "running":
        # No Stop fired for the task that was running: the turn was cut by Esc (D5).
        running = s.get(s.running_task) if s.running_task else None
        if running is not None and running.status == "running":
            running.status = "interrupted"
            interrupted = running.id
        dropped = [t for t in s.tasks if t.status in ("queued", SHADOW_QUEUED)]
        for t in dropped:
            t.status = "dropped"
        notice = _esc_notice(dropped)

    s.state = "running"
    s.current_prompt_id = prompt_id
    s.interrupt = False
    t = s.new_task(prompt, [], prompt_id)
    t.status = "running"
    s.running_task = t.id

    log.record(
        s.session_id, "intake",
        decision="new_turn",
        task=t.id,
        prompt_id=prompt_id,
        interrupted=interrupted,
        dropped=[d.id for d in dropped],
    )
    if notice is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": notice}}


def _classifier_payload(s: store.Store, event: dict, text: str) -> dict:
    running = s.get(s.running_task) if s.running_task else None
    running_dict = {"id": running.id, "text": running.text} if running else None
    queue = [
        {"id": t.id, "text": t.text, "status": t.status, "depends_on": list(t.depends_on)}
        for t in s.tasks if t.status == "queued"
    ]
    recent = transcript.recent(event.get("transcript_path"))
    return classify.payload(s.state, running_dict, queue, recent, text)


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
    """M0 dependency rule: the last queued task, else the running task, else nothing."""
    queued = [t for t in s.tasks if t.status in ("queued", SHADOW_QUEUED)]
    if queued:
        return [queued[-1].id]
    if s.running_task:
        return [s.running_task]
    return []


def handle(event: dict, classifier: Classifier = classify.run, shadow: bool = False) -> dict | None:
    """Sort one UserPromptSubmit event. Returns the hook output or None to pass through."""
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    prompt_id = event.get("prompt_id")

    v = rules.decide(prompt)
    if v.label == "pass":
        return None

    with store.locked(session_id) as s:
        mid_turn = s.state == "running" and prompt_id == s.current_prompt_id
        if not mid_turn:
            return _new_turn(s, event, prompt, prompt_id)

        decision: classify.Decision | None = None
        compared: classify.Decision | None = None
        latency_ms: int | None = None
        if v.label == "undecided":
            decision, latency_ms = _timed(classifier, _classifier_payload(s, event, v.text))
            if (
                decision is not None
                and decision.confidence >= CONFIDENCE_THRESHOLD
                and decision.label in ACTIONABLE_LABELS
            ):
                label = decision.label
            else:
                label = "steer"
        else:
            label = v.label
            if shadow and hookio.shadow_compare():
                # Shadow comparison: also ask the model about a message the
                # rules already decided, so the log holds both answers. The
                # acted label stays the rule's; ``compared`` is never acted on.
                compared, latency_ms = _timed(classifier, _classifier_payload(s, event, v.text), swallow=True)

        logged = decision if decision is not None else compared
        log.record(
            session_id, "intake",
            message=prompt,
            rule=v.rule,
            rule_label=v.label,
            classifier=dataclasses.asdict(logged) if logged is not None else None,
            label=label,
            latency_ms=latency_ms,
            shadow=shadow,
            prompt_id=prompt_id,
        )

        if label == "interrupt":
            if shadow:
                return None
            s.interrupt = True
            return _block(f"{PREFIX}: stopping {s.running_task or 'the current turn'} at the next tool call")

        if label == "queue":
            deps = _chain_deps(s)
            if decision is not None and decision.depends_on:
                deps = list(decision.depends_on)
            t = s.new_task(v.text, deps, prompt_id)
            if shadow:
                # Record what would have been queued so dispatch can report what
                # it would have run (docs/m0-acceptance.md, issue 2). Never ready().
                t.status = SHADOW_QUEUED
                return None
            after = ", ".join(deps) if deps else "nothing"
            return _block(f"{PREFIX}: queued {t.id} after {after}: {t.text}")

        return None
