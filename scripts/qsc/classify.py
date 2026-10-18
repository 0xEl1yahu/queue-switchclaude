"""Classifier wrapper: shells out to `claude -p` with a JSON schema. Stdlib only.

Rules-first intake sends only what it cannot decide here. Every failure
(missing binary, timeout, bad exit, unparseable or invalid answer) returns
``None`` so the caller fails open and treats the message as ``steer``.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

LABELS = ("steer", "queue", "interrupt", "unclear")

SCHEMA: dict = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["label", "confidence", "depends_on"],
    "additionalProperties": False,
}

# scripts/qsc/classify.py -> scripts/ -> repo root -> prompts/intake_system.md
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "intake_system.md"


@dataclass
class Decision:
    label: str
    confidence: float
    depends_on: list[str]


def payload(state: str, running_task: dict | None, queue: list[dict], recent: list[dict], message: str) -> dict:
    """The JSON the model sees.

    ``queue`` items are ``{id, text, status, depends_on}``; ``recent`` is ``[{role, text}]``.
    """
    return {
        "state": state,
        "running_task": running_task,
        "queue": list(queue),
        "recent": list(recent),
        "message": message,
    }


def _system_prompt() -> str | None:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return None


def _validate(obj: object) -> Decision | None:
    """Validate the model's answer against SCHEMA by hand."""
    if not isinstance(obj, dict):
        return None
    if set(obj) - {"label", "confidence", "depends_on"}:
        return None
    label = obj.get("label")
    confidence = obj.get("confidence")
    depends_on = obj.get("depends_on")
    if label not in LABELS:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not 0 <= confidence <= 1:
        return None
    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
        return None
    return Decision(label, float(confidence), list(depends_on))


def run(payload: dict, timeout: float = 15.0, claude: str = "claude") -> Decision | None:
    """Call the isolated `claude -p` classifier. ``None`` on any failure."""
    prompt = _system_prompt()
    if prompt is None:
        return None
    try:
        payload_json = json.dumps(payload)
    except (TypeError, ValueError):
        return None

    cmd = [
        claude, "-p",
        "--model", "haiku",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--tools", "",
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        "--system-prompt", prompt,
        payload_json,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    try:
        out = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(out, dict):
        return None
    return _validate(out.get("structured_output"))
