import pytest

from qsc import rules


@pytest.mark.parametrize("msg,label,rule", [
    ("<system-reminder>x</system-reminder>", "pass", "harness_traffic"),
    ("ok", "pass", "non_task"), ("/compact", "pass", "non_task"), (".", "pass", "non_task"),
    ("stop", "interrupt", "interrupt"), ("Stop that now", "interrupt", "interrupt"), ("don't do that", "interrupt", "interrupt"),
    ("don't stop the dev server", "undecided", None),
    ("p2: add a /login endpoint", "queue", "queue_marker"),
    ("later: email verification", "queue", "queue_marker"),
    ("once that's done, write the alembic migration for it", "queue", "queue_marker"),
    ("when you're done, list the files", "queue", "queue_marker"),
    ("use bcrypt for the hashing", "undecided", None),
    ("add password reset as well", "undecided", None),
])
def test_rules(msg, label, rule):
    v = rules.decide(msg); assert (v.label, v.rule) == (label, rule)


def test_queue_marker_strips_prefix():
    assert rules.decide("p2: add a /login endpoint").text == "add a /login endpoint"
    assert rules.decide("once that's done, write the migration").text == "write the migration"


def test_paste_guard_never_queues():
    big = "\n".join(f"{i}) later: thing {i}" for i in range(13))
    assert rules.decide(big).label == "undecided"


def test_bare_markers_need_a_word_boundary():
    assert rules.decide("thenceforth we ride").label == "undecided"
    assert rules.decide("after thatcher resigned").label == "undecided"
    assert rules.decide("then: run the tests").text == "run the tests"
