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


@pytest.mark.parametrize("msg,label,rule,extra", [
    ("queue: list", "control", "control", "list"),
    ("queue:", "control", "control", ""),
    ("/queue cancel p2", "control", "control", "cancel p2"),
    ("/queue-switchclaude:queue promote p3", "control", "control", "promote p3"),
    ("maybe later we can add social login", "queue", "parked_marker", "we can add social login"),
    ("someday: dark mode", "queue", "parked_marker", "dark mode"),
    ("maybe later we can add this", "undecided", "parked_marker", None),
    ("1) login endpoint 2) logout endpoint 3) tests for both", "undecided", "numbered_split", None),
    ("later: email verification", "queue", "queue_marker", "email verification"),
])
def test_rules_v2(msg, label, rule, extra):
    v = rules.decide(msg)
    assert (v.label, v.rule) == (label, rule)
    if label == "control": assert v.control == extra
    if rule == "parked_marker" and label == "queue": assert v.parked and v.text == extra


def test_slash_other_commands_still_pass():
    assert rules.decide("/compact").label == "pass"


@pytest.mark.parametrize("msg,control", [
    ("Queue: List", "List"),
    ("QUEUE", ""),
    ("/queue", ""),
    ("queue : cancel p2", "cancel p2"),
    ("<system-reminder>queue: list</system-reminder>", None),
])
def test_control_prefix_variants(msg, control):
    v = rules.decide(msg)
    if control is None:
        assert v.label == "pass"
    else:
        assert v.label == "control" and v.rule == "control" and v.control == control


def test_control_needs_the_whole_word():
    assert rules.decide("queued the migration already").label != "control"
    assert rules.decide("queues are backing up").label != "control"


def test_verdict_defaults_are_unflagged():
    v = rules.decide("later: email verification")
    assert v.parked is False and v.control is None
    assert rules.decide("stop").parked is False


@pytest.mark.parametrize("msg,text", [
    ("Maybe later, add social login", "add social login"),
    ("at some point: dark mode", "dark mode"),
    ("one day we could support SSO", "we could support SSO"),
    ("if we have time, polish the readme", "polish the readme"),
])
def test_parked_marker_variants(msg, text):
    v = rules.decide(msg)
    assert (v.label, v.rule, v.parked, v.text) == ("queue", "parked_marker", True, text)


@pytest.mark.parametrize("msg", ["maybe later", "someday:", "maybe later that", "at some point, it.", "someday we should do that"])
def test_parked_marker_without_content_is_undecided(msg):
    v = rules.decide(msg)
    assert (v.label, v.rule, v.parked) == ("undecided", "parked_marker", False)


def test_numbered_split_needs_two_items_and_stays_under_paste_guard():
    assert rules.decide("1) login endpoint").rule is None
    assert rules.decide("- login endpoint\n- logout endpoint").rule == "numbered_split"
    assert rules.decide("* login\n* logout").rule == "numbered_split"
    assert rules.decide("use python 3.10 and 3.11").rule is None
    big = "\n".join(f"{i}) thing {i}" for i in range(13))
    assert rules.decide(big).rule == "paste_guard"


def test_interrupt_and_markers_beat_numbered_split():
    assert rules.decide("later: 1) login 2) logout").rule == "queue_marker"
    assert rules.decide("someday: 1) login 2) logout").rule == "parked_marker"


# Task 9: the rules must never contradict the golden set.

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = [json.loads(l) for l in open(ROOT / "eval" / "golden_queue_intake.jsonl") if l.strip()]


def test_golden_rule_decisions_agree_with_expected():
    decided = {}
    for r in GOLDEN:
        v = rules.decide(r["message"])
        if v.label == "undecided":
            continue
        decided[r["id"]] = v
        accepted = [r["expected"], *r.get("also_acceptable", [])]
        if v.label == "queue":
            assert any(e["label"] == "queue" and bool(e.get("parked")) == v.parked for e in accepted), (r["id"], v)
        elif v.label == "interrupt":
            # g03's expected object carries no flag (docs/m1-eval.md, disputed cases); the label still agrees.
            assert r["expected"]["label"] == "steer", r["id"]
        else:
            raise AssertionError((r["id"], v))
    assert set(decided) == {"g03", "g05", "g06", "g29", "g30", "g33", "g35"}
    assert decided["g33"].parked and decided["g33"].text == "we can add social login"


def test_golden_undecided_cases_keep_their_text():
    # The classifier sees the message unchanged whenever no marker fired.
    for r in GOLDEN:
        v = rules.decide(r["message"])
        if v.rule in (None, "numbered_split", "paste_guard"):
            assert v.text == r["message"], r["id"]


@pytest.mark.parametrize("msg,label,control", [
    ("queue the login endpoint first", "undecided", None),
    ("Queue up a migration after this", "undecided", None),
    ("queue list", "control", "list"),
    ("queue cancel p2", "control", "cancel p2"),
    ("queue", "control", ""),
    ("queue: the login endpoint first", "control", "the login endpoint first"),
])
def test_control_rule_requires_colon_slash_or_command_word(msg, label, control):
    v = rules.decide(msg)
    assert v.label == label and v.control == control


# --- M2 Task 6: approval words ------------------------------------------------

def test_is_approval_words():
    assert rules.is_approval("LGTM!") and rules.is_approval("ship it") and not rules.is_approval("looks good but do X first")


@pytest.mark.parametrize("msg", ["ok", "Okay.", "yes", "y", "go", "Go ahead", "approve", "approved", "do it", "looks good", "sounds good", "  LGTM  "])
def test_is_approval_accepts_every_word(msg):
    assert rules.is_approval(msg)


@pytest.mark.parametrize("msg", ["ok, merge", "yes but use postgres", "go on", "continue", ".", "", "lgtm, and add logout"])
def test_is_approval_is_exact(msg):
    assert not rules.is_approval(msg)


@pytest.mark.parametrize("msg,expected", [
    ("ok, merge", True), ("ok", True), ("LGTM! merge it", True), ("yes: ship", True),
    ("ok, but use postgres", False), ("ok, except the tests", False), ("yes, however keep sqlite", False),
    ("use postgres", False), ("looks good but do X first", False), ("merge, ok", False),
])
def test_is_confirmation(msg, expected):
    assert rules.is_confirmation(msg) is expected


def test_approval_words_are_a_frozenset_including_the_non_task_words():
    assert isinstance(rules.APPROVE_WORDS, frozenset)
    assert {"ok", "okay", "yes", "y", "go"} <= rules.APPROVE_WORDS


def test_bare_control_words_include_m2_commands():
    assert rules.decide("queue approve").control == "approve"
    assert rules.decide("queue show p2").control == "show p2"


# --- M3 Task 4: more harness traffic -----------------------------------------

@pytest.mark.parametrize("msg", [
    "[Request interrupted by user for tool use]",
    "<task-notification>\n<task-id>x</task-id>",
    "Base directory for this skill: /x/y\n\n# Brainstorming",
    "<command-message>foo</command-message>\n<command-name>foo</command-name>",
])
def test_more_harness_traffic_passes(msg):
    assert rules.decide(msg).rule == "harness_traffic"


@pytest.mark.parametrize("msg", [
    "[Request interrupted by user]",
    "  <side-sessions-event>x</side-sessions-event>",
    "<ci-monitor-event>run 12 failed</ci-monitor-event>",
    "Another Claude session sent a message: hi",
    "[Subagent hand-back] all done",
])
def test_every_harness_phrase_passes(msg):
    v = rules.decide(msg)
    assert (v.label, v.rule) == ("pass", "harness_traffic")


def test_harness_phrases_table_has_the_new_entries():
    for phrase in ("[Request interrupted by user", "<task-notification>", "<side-sessions-event>",
                   "<ci-monitor-event>", "<command-message>", "<command-name>", "Base directory for this skill:"):
        assert phrase in rules.HARNESS_PHRASES


def test_harness_phrase_only_counts_in_the_first_200_characters():
    filler = "write a note that says " + "x" * 200
    assert rules.decide(filler + " [Request interrupted by user]").rule != "harness_traffic"
    assert rules.decide("[Request interrupted by user] " + filler).rule == "harness_traffic"


def test_harness_traffic_beats_control_and_markers():
    assert rules.decide("<command-message>queue: list</command-message>").rule == "harness_traffic"
    assert rules.decide("[Request interrupted by user] later: add logout").rule == "harness_traffic"
    assert rules.decide("<task-notification>stop</task-notification>").rule == "harness_traffic"
