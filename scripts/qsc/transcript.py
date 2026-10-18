"""Best-effort tail of a Claude Code session transcript. Stdlib only.

The transcript is a JSONL file named by the hook payload's ``transcript_path``.
Only the last ``max_bytes`` are read, so the first line seen may be a partial
one and is dropped. Anything unreadable yields an empty list: the transcript is
context for the classifier, never a hard dependency.
"""
from __future__ import annotations

import json
import os

_ROLES = ("user", "assistant")


def _text_of(entry: dict) -> str | None:
    """Flatten an entry's message content to a string, or None when absent."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return None


def recent(path: str | None, n: int = 4, max_bytes: int = 65536) -> list[dict]:
    """Last ``n`` user/assistant turns of the transcript as ``[{"role", "text"}]``.

    Reads at most the last ``max_bytes`` of the file. Entries whose text
    starts with ``<`` (system reminders, harness traffic) are skipped, as are
    entries with no text at all. Returns ``[]`` when ``path`` is None, missing
    or unreadable.
    """
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()
    except (OSError, ValueError):
        return []

    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line is almost certainly cut mid-way

    turns: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        role = entry.get("type")
        if role not in _ROLES:
            continue
        text = _text_of(entry)
        if text is None:
            continue
        if text.lstrip().startswith("<") or not text.strip():
            continue
        turns.append({"role": role, "text": text})

    return turns[-n:] if n > 0 else []
