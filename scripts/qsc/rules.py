"""Deterministic intake rules (decision D2b: rules first, model second).

`decide(message)` applies a fixed, ordered rule table and returns a Verdict.
First match wins. Anything the table cannot decide is `undecided`, which the
intake script then hands to the classifier. A rule never queues a pasted
block (150+ words or 12+ lines) and never interrupts on a sentence that
merely contains "stop": only a message that *is* a stop phrase interrupts.

Rule order and labels:

    harness_traffic  -> pass
    non_task         -> pass
    paste_guard      -> undecided
    interrupt        -> interrupt
    queue_marker     -> queue   (text = the captured remainder)
    (none)           -> undecided

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

LABELS = ("steer", "queue", "interrupt", "pass", "undecided")

HARNESS_PHRASES = (
    "Another Claude session sent a message",
    "[Subagent hand-back]",
)

NON_TASK_WORDS = frozenset({"ok", "okay", "yes", "y", "continue", "go", "."})

PASTE_WORDS = 150
PASTE_LINES = 12

_INTERRUPT_RE = re.compile(
    r"^(stop|halt|abort)( (that|this|it|now|everything))*( now)?$"
)
_INTERRUPT_PHRASES = frozenset({"don't do that", "do not do that"})

# Trailing punctuation stripped before the interrupt match ("stop!", "Stop.").
_TRAILING_PUNCT = ".,;:!?"

_QUEUE_MARKER_RES = (
    re.compile(r"^p\d+:\s*(.+)$", re.IGNORECASE | re.DOTALL),
    re.compile(r"^later:\s*(.+)$", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"^(once|when|after) (that|this|it|you)('s|'re|'ve| is| are| have)? "
        r"(done|finished|in|through)[,:]?\s*(.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"^after that\b[,:]?\s*(.+)$", re.IGNORECASE | re.DOTALL),
    re.compile(r"^then\b[,:]?\s*(.+)$", re.IGNORECASE | re.DOTALL),
)


@dataclass
class Verdict:
    """Outcome of the rule table for one message.

    label: one of LABELS.
    rule:  name of the rule that fired, or None when no rule matched.
    text:  the task text with any marker stripped (the message itself when
           no marker applies).
    """

    label: str
    rule: Optional[str]
    text: str


def _normalise(message: str) -> str:
    """Lowercase, collapse whitespace, straighten curly apostrophes."""
    text = message.replace("’", "'").replace("‘", "'")
    return " ".join(text.split()).lower()


def _is_harness_traffic(message: str) -> bool:
    if message.lstrip().startswith("<"):
        return True
    return any(phrase in message for phrase in HARNESS_PHRASES)


def _is_non_task(message: str) -> bool:
    stripped = message.strip()
    if stripped.startswith("/"):
        return True
    return stripped.lower() in NON_TASK_WORDS


def _is_paste(message: str) -> bool:
    words = len(message.split())
    lines = len(message.strip().splitlines())
    return words >= PASTE_WORDS or lines >= PASTE_LINES


def _is_interrupt(message: str) -> bool:
    phrase = _normalise(message).rstrip(_TRAILING_PUNCT + " ")
    if phrase in _INTERRUPT_PHRASES:
        return True
    return _INTERRUPT_RE.match(phrase) is not None


def _queue_remainder(message: str) -> Optional[str]:
    text = message.strip().replace("’", "'").replace("‘", "'")
    for pattern in _QUEUE_MARKER_RES:
        m = pattern.match(text)
        if m:
            remainder = m.group(m.lastindex).strip()
            if remainder:
                return remainder
    return None


def decide(message: str) -> Verdict:
    """Apply the rule table to `message`; first match wins."""
    if not isinstance(message, str):
        message = "" if message is None else str(message)

    if _is_harness_traffic(message):
        return Verdict("pass", "harness_traffic", message)

    if _is_non_task(message):
        return Verdict("pass", "non_task", message)

    if _is_paste(message):
        return Verdict("undecided", "paste_guard", message)

    if _is_interrupt(message):
        return Verdict("interrupt", "interrupt", message)

    remainder = _queue_remainder(message)
    if remainder is not None:
        return Verdict("queue", "queue_marker", remainder)

    return Verdict("undecided", None, message)
