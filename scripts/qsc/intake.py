"""Intake logic for the ``UserPromptSubmit`` hook. Stdlib only.

``handle(event)`` sorts one user message into steer / queue / interrupt:
rules first (``qsc.rules``), the ``claude -p`` classifier (``qsc.classify``)
only for what the rules cannot decide. It also keeps the per-session store
in step with the turn: a message whose ``prompt_id`` differs from the one
being run starts a new turn, and a new turn arriving while the store still
says ``running`` means the previous turn was interrupted by Esc (D5): the
running task is marked ``interrupted``, queued tasks are ``dropped``, and a
notice listing them is attached to the new prompt.

The return value is the hook's JSON output, or ``None`` for pass-through.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Optional

from . import classify, log, rules, store, transcript

HOOK_EVENT = "UserPromptSubmit"
PREFIX = "queue-switchclaude"

# The model's answer counts only at or above this confidence (spec §10 M0).
CONFIDENCE_THRESHOLD = 0.9
ACTIONABLE_LABELS = ("queue", "interrupt")

Classifier = Callable[[dict], Optional[classify.Decision]]


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
        dropped = [t for t in s.tasks if t.status == "queued"]
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


def _chain_deps(s: store.Store) -> list[str]:
    """M0 dependency rule: the last queued task, else the running task, else nothing."""
    queued = [t for t in s.tasks if t.status == "queued"]
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
        if v.label == "undecided":
            decision = classifier(_classifier_payload(s, event, v.text))
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

        log.record(
            session_id, "intake",
            message=prompt,
            rule=v.rule,
            classifier=dataclasses.asdict(decision) if decision is not None else None,
            label=label,
            shadow=shadow,
            prompt_id=prompt_id,
        )

        if shadow:
            return None

        if label == "interrupt":
            s.interrupt = True
            return {"decision": "block", "reason": f"{PREFIX}: stopping at the next tool call"}

        if label == "queue":
            deps = _chain_deps(s)
            if decision is not None and decision.depends_on:
                deps = list(decision.depends_on)
            t = s.new_task(v.text, deps, prompt_id)
            after = ", ".join(deps) if deps else "nothing"
            return {"decision": "block", "reason": f"{PREFIX}: queued {t.id} after {after}: {t.text}"}

        return None
