"""Classifier wrapper: `claude -p` with the Decision JSON schema. Stdlib only.

Rules-first intake sends only what it cannot decide here. The call goes
through ``model.call`` (shared with the M2 done judge); every failure
(missing binary, timeout, bad exit, unparseable or invalid answer) returns
``None`` so the caller fails open and treats the message as ``steer``.

Decision contract v2 (M1): the model answers in the golden set's ``expected``
shape. ``label`` is one of LABELS; ``interrupt`` and ``parked`` are flags
(on ``steer`` and ``queue`` respectively), ``target`` names the task an
``amend`` changes, ``note`` carries the amend/steer qualifier, and ``parts``
holds the per-part decisions of a ``split``.
"""
from __future__ import annotations

import subprocess  # noqa: F401  (tests monkeypatch ``classify.subprocess.run``; it is the module ``model`` uses)
from dataclasses import dataclass, field
from pathlib import Path

from . import model

LABELS =("steer", "queue", "amend", "split", "unclear")

# Values `note` may take; None means no note.
NOTES = ("approve", "revise plan", "cancel", "pause")

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NOTE_SCHEMA = {"anyOf": [{"type": "string", "enum": list(NOTES)}, {"type": "null"}]}
_CONFIDENCE_SCHEMA = {"type": "number", "minimum": 0, "maximum": 1}
_DEPENDS_ON_SCHEMA = {"type": "array", "items": {"type": "string"}}

PART_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "text": {"type": "string"},
        "confidence": _CONFIDENCE_SCHEMA,
        "interrupt": {"type": "boolean"},
        "parked": {"type": "boolean"},
        "depends_on": _DEPENDS_ON_SCHEMA,
        "target": _NULLABLE_STRING,
        "note": _NOTE_SCHEMA,
    },
    "required": ["label", "text", "confidence", "interrupt", "parked", "depends_on", "target", "note"],
    "additionalProperties": False,
}

SCHEMA: dict = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "confidence": _CONFIDENCE_SCHEMA,
        "interrupt": {"type": "boolean"},
        "parked": {"type": "boolean"},
        "depends_on": _DEPENDS_ON_SCHEMA,
        "target": _NULLABLE_STRING,
        "note": _NOTE_SCHEMA,
        "parts": {"type": "array", "items": PART_SCHEMA},
    },
    "required": ["label", "confidence", "interrupt", "parked", "depends_on", "target", "note", "parts"],
    "additionalProperties": False,
}

# scripts/qsc/classify.py -> scripts/ -> repo root -> prompts/intake_system.md
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "intake_system.md"


@dataclass
class Part:
    """One part of a ``split`` decision: the text of the part and its own decision."""
    label: str
    text: str
    confidence: float
    interrupt: bool = False
    parked: bool = False
    depends_on: list[str] = field(default_factory=list)
    target: str | None = None
    note: str | None = None


@dataclass
class Decision:
    """The classifier's answer for one message.

    The first three fields keep the M0 positional order
    (``Decision(label, confidence, depends_on)``); the v2 fields default so
    M0 callers and tests build a valid v2 decision unchanged.
    """
    label: str
    confidence: float
    depends_on: list[str] = field(default_factory=list)
    interrupt: bool = False
    parked: bool = False
    target: str | None = None
    note: str | None = None
    parts: list[Part] = field(default_factory=list)


def payload(
    state: str,
    running_task: dict | None,
    queue: list[dict],
    recent: list[dict],
    message: str,
    candidates: list[dict] | None = None,
) -> dict:
    """The JSON the model sees.

    ``queue`` items are ``{id, text, status, depends_on}``; ``recent`` is
    ``[{role, text}]``; ``candidates``, when given, is ``[{id, text, score}]``,
    the tasks most similar to the message (amend targets).
    """
    out = {
        "state": state,
        "running_task": running_task,
        "queue": list(queue),
        "recent": list(recent),
        "message": message,
    }
    if candidates is not None:
        out["candidates"] = list(candidates)
    return out


def _system_prompt() -> str | None:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return None


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 1:
        return None
    return float(value)


def _depends_on(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        return None
    return list(value)


def _common(obj: dict) -> dict | None:
    """Validate the fields shared by Decision and Part; None when any is wrong."""
    label = obj.get("label")
    if label not in LABELS:
        return None
    confidence = _confidence(obj.get("confidence"))
    if confidence is None:
        return None
    interrupt = obj.get("interrupt")
    parked = obj.get("parked")
    if not isinstance(interrupt, bool) or not isinstance(parked, bool):
        return None
    depends_on = _depends_on(obj.get("depends_on"))
    if depends_on is None:
        return None
    target = obj.get("target")
    if target is not None and not isinstance(target, str):
        return None
    note = obj.get("note")
    if note is not None and note not in NOTES:
        return None
    return {
        "label": label,
        "confidence": confidence,
        "interrupt": interrupt,
        "parked": parked,
        "depends_on": depends_on,
        "target": target,
        "note": note,
    }


def _validate_part(obj: object) -> Part | None:
    if not isinstance(obj, dict):
        return None
    if set(obj) != set(PART_SCHEMA["required"]):
        return None
    common = _common(obj)
    if common is None:
        return None
    text = obj.get("text")
    if not isinstance(text, str):
        return None
    return Part(text=text, **common)


def _validate(obj: object) -> Decision | None:
    """Validate the model's answer against SCHEMA by hand.

    Every key must be present, no extra key is allowed, and every value must
    have the schema's type (``null`` only for ``target`` and ``note``).
    """
    if not isinstance(obj, dict):
        return None
    if set(obj) != set(SCHEMA["required"]):
        return None
    common = _common(obj)
    if common is None:
        return None
    raw_parts = obj.get("parts")
    if not isinstance(raw_parts, list):
        return None
    parts = []
    for raw in raw_parts:
        part = _validate_part(raw)
        if part is None:
            return None
        parts.append(part)
    return Decision(parts=parts, **common)


def run(payload: dict, timeout: float = 15.0, claude: str = "claude") -> Decision | None:
    """Call the isolated `claude -p` classifier. ``None`` on any failure.

    The call itself is ``model.call`` (M2: one wrapper shared with the judge);
    this function owns the prompt, the schema and the validation.
    """
    out = model.call(PROMPT_PATH, SCHEMA, payload, timeout, claude)
    if out is None:
        return None
    return _validate(out)
