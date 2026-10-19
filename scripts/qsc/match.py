"""Amend target matching: which queued task does a message talk about?

Stdlib only (``difflib`` and token sets; no rapidfuzz, no embeddings). Intake
calls ``pick`` when the classifier said ``amend`` without naming a target the
store knows, and ``candidates`` to put the closest tasks into the classifier
payload. A wrong target would edit or cancel the wrong task, so ``pick`` is
conservative: it answers only when the best task clears ``accept`` *and* beats
the runner-up by ``margin`` (plan review focus 2). Everything else is ``None``
and intake falls through to the model, and from there to ``steer``.

``score`` is the best of three views of the same pair, each in [0, 1]:

* ``ratio``   -- ``difflib.SequenceMatcher`` on the normalised strings
                (catches near-verbatim resubmissions and short strings).
* ``jaccard`` -- token-set Jaccard, tokens being words of 3+ letters minus
                a small stopword set.
* ``subject`` -- how much of the task's *subject* (its content words minus
                generic action verbs such as "add" or "write") the message
                mentions. A user names a task by its subject and then adds
                new material ("for the tests, include a rate-limit case"), so
                the message side is expected to carry unmatched words and
                only the task side is scored. Coverage ``c`` is Dice-smoothed
                to ``2c / (1 + c)``: naming one word of a two-word subject
                scores 0.67, one word of a three-word subject 0.5.

The plan's bare ``max(ratio, jaccard)`` scores g23 at 0.46 and g14 at 0.39,
below the 0.6 accept it also fixes; the subject view is what lets those two
cases resolve without lowering the thresholds.
"""
from __future__ import annotations

import difflib
import re

from . import store

# The plan's stopword set, applied to the token view only.
STOPWORDS = frozenset("the a an to for of in on and or it this that with".split())

# Action verbs and filler that say what to do with a subject, not what it is.
# Removed from the task side of the subject view only.
GENERIC_WORDS = frozenset((
    "add", "write", "create", "make", "build", "implement", "refactor", "fix",
    "update", "remove", "delete", "drop", "use", "run", "set", "change", "move",
    "rename", "put", "get", "also", "should", "please", "then", "now", "new",
    "into", "its", "own", "all", "some",
))

_WORD_RE = re.compile(r"[a-z]{3,}")


def _stem(word: str) -> str:
    """Drop a plural/3rd-person ``s`` so "tests" meets "test" and "prints" meets "print"."""
    if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> set[str]:
    """Words of 3+ letters in the normalised text, lightly stemmed, minus STOPWORDS."""
    return {_stem(w) for w in _WORD_RE.findall(store.normalise(text)) if w not in STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _subject(message_tokens: set[str], task_tokens: set[str]) -> float:
    subject = task_tokens - GENERIC_WORDS
    if not subject:
        return 0.0
    coverage = len(subject & message_tokens) / len(subject)
    return 2 * coverage / (1 + coverage)


def score(message: str, task_text: str) -> float:
    """Similarity of ``message`` to one task text, in [0, 1]; 0.0 when either is empty."""
    a, b = store.normalise(message), store.normalise(task_text)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = tokens(a), tokens(b)
    best = max(ratio, _jaccard(ta, tb), _subject(ta, tb))
    return min(1.0, max(0.0, best))


def rank(message: str, tasks: list[store.Task]) -> list[tuple[store.Task, float]]:
    """Every task paired with its score, best first; ties keep creation order."""
    scored = [(t, score(message, t.text)) for t in tasks]
    scored.sort(key=lambda pair: pair[1], reverse=True)   # sort is stable
    return scored


def pick(
    message: str,
    tasks: list[store.Task],
    accept: float = 0.6,
    margin: float = 0.2,
) -> store.Task | None:
    """The one task the message clearly names, or ``None``.

    The top task is returned only when its score is at least ``accept`` and it
    beats the runner-up by at least ``margin`` (or there is no runner-up).
    """
    ranked = rank(message, tasks)
    if not ranked:
        return None
    top, top_score = ranked[0]
    if top_score < accept:
        return None
    if len(ranked) > 1 and top_score - ranked[1][1] < margin:
        return None
    return top


def candidates(message: str, tasks: list[store.Task], n: int = 3) -> list[dict]:
    """The ``n`` closest tasks as ``{id, text, score}`` for the classifier payload."""
    return [
        {"id": t.id, "text": t.text, "score": round(s, 3)}
        for t, s in rank(message, tasks)[:max(0, n)]
    ]
