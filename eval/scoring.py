"""Scoring one harness decision against a golden case (spec section 8). Stdlib only.

``outcome(expected, got)`` compares two dicts in the golden ``expected`` shape
and returns ``(score, reason)``:

- ``(1.0, "ok")`` when every relevant key matches (``confidence`` and ``id``
  are not compared; ids are assigned deterministically by the runner).
- ``(-1.0, "missed_hard_dependency")`` when a ``queue`` decision lacks a
  dependency the golden case has: the task would run before something it
  needs. This is the asymmetric part: a spurious dependency only delays the
  task, so it scores ``0.0`` with reason ``dependency_extra``.
- ``(0.0, reason)`` otherwise, ``reason`` naming the first key that differs:
  ``label``, ``dependency_extra``, ``parked``, ``target``, ``note``, ``parts``
  or ``interrupt``.

Split cases compare part by part, in order; a dependency miss inside any part
is a miss for the case.
"""
from __future__ import annotations

REASONS = (
    "ok", "missed_hard_dependency", "label", "dependency_extra", "parked",
    "target", "note", "parts", "interrupt",
)


def _deps(d: dict) -> list[str]:
    deps = d.get("depends_on")
    return list(deps) if isinstance(deps, list) else []


def outcome(expected: dict, got: object) -> tuple[float, str]:
    """Score ``got`` (the harness decision) against one golden ``expected`` object."""
    if not isinstance(got, dict) or not isinstance(expected, dict):
        return (0.0, "label")
    label = expected.get("label")
    if got.get("label") != label:
        return (0.0, "label")

    if label == "queue":
        exp, act = _deps(expected), _deps(got)
        if any(d not in act for d in exp):
            return (-1.0, "missed_hard_dependency")
        if any(d not in exp for d in act):
            return (0.0, "dependency_extra")
        if bool(expected.get("parked", False)) != bool(got.get("parked", False)):
            return (0.0, "parked")
        return (1.0, "ok")

    if label == "amend":
        if expected.get("target") != got.get("target"):
            return (0.0, "target")
        if expected.get("note") != got.get("note"):
            return (0.0, "note")
        return (1.0, "ok")

    if label == "split":
        exp_parts = expected.get("parts")
        got_parts = got.get("parts")
        if not isinstance(exp_parts, list) or not isinstance(got_parts, list):
            return (0.0, "parts")
        if len(exp_parts) != len(got_parts):
            return (0.0, "parts")
        worst = 1.0
        for e, g in zip(exp_parts, got_parts):
            score, _ = outcome(e, g)
            if score < 0:
                return (-1.0, "missed_hard_dependency")
            worst = min(worst, score)
        return (1.0, "ok") if worst == 1.0 else (0.0, "parts")

    # steer (and any other label): the flags and the note must agree.
    if bool(expected.get("interrupt", False)) != bool(got.get("interrupt", False)):
        return (0.0, "interrupt")
    if expected.get("note") != got.get("note"):
        return (0.0, "note")
    return (1.0, "ok")


def score_case(row: dict, got: object) -> tuple[float, str]:
    """Best outcome across ``row["expected"]`` and every ``row["also_acceptable"]`` entry."""
    alternatives = [row["expected"], *row.get("also_acceptable", [])]
    best: tuple[float, str] | None = None
    for expected in alternatives:
        result = outcome(expected, got)
        if best is None or result[0] > best[0]:
            best = result
        if best[0] == 1.0:
            break
    return best if best is not None else (0.0, "label")
