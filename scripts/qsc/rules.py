"""Deterministic intake rules (decision D2b: rules first, model second).

`decide(message)` applies a fixed, ordered rule table and returns a Verdict.
First match wins. Anything the table cannot decide is `undecided`, which the
intake script then hands to the classifier. A rule never queues a pasted
block (150+ words or 12+ lines) and never interrupts on a sentence that
merely contains "stop": only a message that *is* a stop phrase interrupts.

Rule order and labels:

    harness_traffic  -> pass
    control          -> control   (control = the command after the prefix)
    non_task         -> pass
    paste_guard      -> undecided
    interrupt        -> interrupt
    parked_marker    -> queue, parked=True (text = the captured remainder);
                        undecided when the remainder has no content
    queue_marker     -> queue   (text = the captured remainder)
    numbered_split   -> undecided (rule name only; the model does the split)
    (none)           -> undecided

The table never looks at the harness state. Two helpers do the state-aware
word matching for intake's waiting states (M2, decision D3): `is_approval`
("lgtm", "ok", "ship it": exact after normalisation, so "ok" approves a
waiting plan but "looks good but do X first" never does) and
`is_confirmation` ("ok, merge": an approval word as the first clause, used
only while the running task waits for the user's answer, spec section 7).

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

LABELS = ("steer", "queue", "interrupt", "control", "pass", "undecided")

HARNESS_PHRASES = (
    "Another Claude session sent a message",
    "[Subagent hand-back]",
)

NON_TASK_WORDS = frozenset({"ok", "okay", "yes", "y", "continue", "go", "."})

# What approves a plan that is waiting (state `awaiting_approval`, g44) or
# confirms a task done (state `awaiting_answer`, spec section 7). Exact match
# after normalisation: "ok" is here, "ok, but ..." is not. The overlap with
# NON_TASK_WORDS is deliberate: intake checks the store state before the
# non-task pass short-circuit, so "ok" approves only while a plan waits.
APPROVE_WORDS = frozenset({
    "lgtm", "approve", "approved", "go", "go ahead", "yes", "ok", "okay", "y",
    "ship it", "do it", "looks good", "sounds good",
})

# "ok, but use postgres" is an answer that happens to start with "ok", not a
# confirmation: a remainder opening with one of these words keeps
# `is_confirmation` False.
CONTRAST_WORDS = frozenset({"but", "except", "however", "though", "although", "unless", "only"})

# "ok, merge" -> head "ok", rest "merge".
_CLAUSE_RE = re.compile(r"^(?P<head>[^,;:!.?]+)[,;:!.?]+\s*(?P<rest>.+)$")

PASTE_WORDS = 150
PASTE_LINES = 12

_INTERRUPT_RE = re.compile(
    r"^(stop|halt|abort)( (that|this|it|now|everything))*( now)?$"
)
_INTERRUPT_PHRASES = frozenset({"don't do that", "do not do that"})

# Trailing punctuation stripped before the interrupt match ("stop!", "Stop.").
_TRAILING_PUNCT = ".,;:!?"

# `queue: list`, `/queue cancel p2`, `/queue-switchclaude:queue promote p3`.
# The remainder is the command; empty means `list`. The word boundary keeps
# "queued ..." and "queues ..." out of the control channel.
# Three forms are control: the slash command, `queue:` with a colon, and a
# bare `queue <command>` whose first word is a known command. Prose that
# merely starts with "queue" ("queue the login endpoint first") is a task.
CONTROL_WORDS = ("list", "cancel", "promote", "park", "drop", "pause", "resume", "help")
_CONTROL_SLASH_RE = re.compile(r"^/queue(-switchclaude:queue)?\b\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_CONTROL_COLON_RE = re.compile(r"^queue\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)
_CONTROL_BARE_RE = re.compile(r"^queue\s+([A-Za-z]+)\b(.*)$", re.IGNORECASE | re.DOTALL)

# Tentative markers: the item is recorded as `parked`, never dispatched
# until promoted (spec section 3, g33).
_PARKED_MARKER_RE = re.compile(
    r"^(maybe later|someday|at some point|one day|if we have time)\b[,:]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)

# A remainder whose object is a bare pronoun has no content of its own
# ("maybe later we can add this", g34): the referent lives in the recent
# turn, which only the classifier sees.
_PRONOUNS = frozenset({"this", "that", "it"})

# Two or more list items mark a probable split; the rule only names it for
# the log, the classifier does the split.
_LIST_ITEM_RE = re.compile(r"(^|\s)(\d+[).]|[-*])\s+\S")

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

    label:   one of LABELS.
    rule:    name of the rule that fired, or None when no rule matched.
    text:    the task text with any marker stripped (the message itself when
             no marker applies).
    parked:  True when a tentative marker fired (label `queue`).
    control: the control command after the `queue:` prefix (label
             `control`), empty for a bare prefix; None otherwise.
    """

    label: str
    rule: Optional[str]
    text: str
    parked: bool = False
    control: Optional[str] = None


def _normalise(message: str) -> str:
    """Lowercase, collapse whitespace, straighten curly apostrophes."""
    text = message.replace("’", "'").replace("‘", "'")
    return " ".join(text.split()).lower()


def _is_harness_traffic(message: str) -> bool:
    if message.lstrip().startswith("<"):
        return True
    return any(phrase in message for phrase in HARNESS_PHRASES)


def _control_command(message: str) -> Optional[str]:
    text = message.strip()
    m = _CONTROL_SLASH_RE.match(text) or _CONTROL_COLON_RE.match(text)
    if m is not None:
        return m.group(m.lastindex).strip()
    if text.lower() == "queue":
        return ""
    m = _CONTROL_BARE_RE.match(text)
    if m is not None and m.group(1).lower() in CONTROL_WORDS:
        return (m.group(1) + m.group(2)).strip()
    return None


def _approval_form(message: str) -> str:
    """The comparison form of a candidate approval: normalised, trailing punctuation gone."""
    if not isinstance(message, str):
        message = "" if message is None else str(message)
    return _normalise(message).rstrip(_TRAILING_PUNCT + " ")


def is_approval(message: str) -> bool:
    """True when the whole message is one of APPROVE_WORDS ("LGTM!", "ship it").

    Exact after normalisation and trailing-punctuation stripping, so any
    added clause ("looks good but do X first", "ok, merge") is not an
    approval; intake sends those to the classifier (revise plan) or treats
    them as a confirmation, by state.
    """
    return _approval_form(message) in APPROVE_WORDS


def is_confirmation(message: str) -> bool:
    """True when the message confirms a task done: an approval, or an approval
    word as the first clause with more after it ("ok, merge", spec section 7).

    A remainder that opens with a contrast word ("ok, but use postgres") is
    an answer, not a confirmation. Used only in state `awaiting_answer`.
    """
    if is_approval(message):
        return True
    m = _CLAUSE_RE.match(_approval_form(message))
    if m is None:
        return False
    head = m.group("head").strip()
    rest = m.group("rest").strip().split()
    if head not in APPROVE_WORDS or not rest:
        return False
    return rest[0] not in CONTRAST_WORDS


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


def _parked_remainder(message: str) -> Optional[str]:
    """The text after a tentative marker, "" when it carries no content."""
    text = message.strip().replace("\u2019", "'").replace("\u2018", "'")
    m = _PARKED_MARKER_RE.match(text)
    if m is None:
        return None
    remainder = m.group(m.lastindex).strip()
    words = _normalise(remainder).rstrip(_TRAILING_PUNCT + " ").split()
    if not words or words[-1] in _PRONOUNS:
        return ""
    return remainder


def _is_numbered_split(message: str) -> bool:
    return len(_LIST_ITEM_RE.findall(message)) >= 2


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

    command = _control_command(message)
    if command is not None:
        return Verdict("control", "control", message, control=command)

    if _is_non_task(message):
        return Verdict("pass", "non_task", message)

    if _is_paste(message):
        return Verdict("undecided", "paste_guard", message)

    if _is_interrupt(message):
        return Verdict("interrupt", "interrupt", message)

    parked = _parked_remainder(message)
    if parked is not None:
        if not parked:
            return Verdict("undecided", "parked_marker", message)
        return Verdict("queue", "parked_marker", parked, parked=True)

    remainder = _queue_remainder(message)
    if remainder is not None:
        return Verdict("queue", "queue_marker", remainder)

    if _is_numbered_split(message):
        return Verdict("undecided", "numbered_split", message)

    return Verdict("undecided", None, message)
