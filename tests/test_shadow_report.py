"""Task 10: eval/shadow_report.py summarises rule-vs-model decisions from the logs."""
import json
import os
import subprocess
import sys
from pathlib import Path

from qsc import log, paths

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "eval" / "shadow_report.py"


def run(data_dir, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})


def test_shadow_report_counts(data_dir):
    log.record("s1", "intake", label="queue", rule="queue_marker", rule_label="queue", classifier={"label": "steer"}, latency_ms=3400)
    log.record("s1", "intake", label="steer", rule=None, classifier={"label": "steer"}, latency_ms=3000)
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "shadow_report.py")], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert "disagreements: 1" in r.stdout and "mean classifier latency: 3200 ms" in r.stdout


def test_shadow_report_rule_vs_model_counts(data_dir):
    log.record("s1", "intake", label="queue", rule="queue_marker", rule_label="queue", classifier=None, latency_ms=None)
    log.record("s1", "intake", label="steer", rule=None, rule_label="undecided", classifier={"label": "steer"}, latency_ms=3000)
    log.record("s2", "intake", label="steer", rule="paste_guard", rule_label="undecided", classifier=None, latency_ms=None)
    log.record("s2", "intake", decision="new_turn", task="p1")   # not a decision line
    r = run(data_dir)
    assert r.returncode == 0
    assert "rule-decided: 1" in r.stdout and "model-decided: 2" in r.stdout and "disagreements: 0" in r.stdout


def test_shadow_report_m0_rows_without_rule_label(data_dir):
    # M0 log lines carry `rule` but no `rule_label`; a fired rule other than paste_guard decided.
    log.record("s1", "intake", label="queue", rule="queue_marker", classifier=None)
    log.record("s1", "intake", label="steer", rule=None, classifier={"label": "steer", "confidence": 0.95})
    r = run(data_dir)
    assert "rule-decided: 1" in r.stdout and "model-decided: 1" in r.stdout


def test_shadow_report_interrupt_flag_is_not_a_disagreement(data_dir):
    # v2 classifier answers "steer" with interrupt=True; the rule said "interrupt". Same decision.
    log.record("s1", "intake", label="interrupt", rule="interrupt", rule_label="interrupt",
               classifier={"label": "steer", "interrupt": True}, latency_ms=100)
    r = run(data_dir)
    assert "disagreements: 0" in r.stdout


def test_shadow_report_lists_recent_disagreements_with_text(data_dir):
    for i in range(12):
        log.record("s1", "intake", message=f"later: thing {i}", label="queue", rule="queue_marker", rule_label="queue",
                   classifier={"label": "steer"}, latency_ms=10)
    r = run(data_dir)
    assert "disagreements: 12" in r.stdout
    assert "later: thing 11" in r.stdout and "later: thing 2" in r.stdout
    assert "later: thing 1\n" not in r.stdout and "later: thing 0" not in r.stdout


def test_shadow_report_empty_data_dir(data_dir):
    r = run(data_dir)
    assert r.returncode == 0 and "no intake decisions" in r.stdout and "Traceback" not in r.stderr


def test_shadow_report_no_latency_logged(data_dir):
    log.record("s1", "intake", label="queue", rule="queue_marker", rule_label="queue", classifier=None)
    r = run(data_dir)
    assert "mean classifier latency: n/a" in r.stdout


def test_shadow_report_since_filters_old_rows(data_dir):
    p = paths.log_path("s1"); p.parent.mkdir(parents=True, exist_ok=True)
    old = {"ts": "2020-01-01T00:00:00+00:00", "event": "intake", "label": "queue", "rule": "queue_marker",
           "rule_label": "queue", "classifier": {"label": "steer"}, "latency_ms": 5000}
    p.write_text(json.dumps(old) + "\n")
    log.record("s1", "intake", label="queue", rule="queue_marker", rule_label="queue", classifier={"label": "queue"}, latency_ms=1000)
    r = run(data_dir, "--since", "7d")
    assert "disagreements: 0" in r.stdout and "mean classifier latency: 1000 ms" in r.stdout
    r = run(data_dir)
    assert "disagreements: 1" in r.stdout and "mean classifier latency: 3000 ms" in r.stdout


def test_shadow_report_bad_since_value_is_an_error_not_a_traceback(data_dir):
    r = run(data_dir, "--since", "soon")
    assert r.returncode == 2 and "Traceback" not in r.stderr


def test_shadow_report_skips_bad_lines(data_dir):
    p = paths.log_path("s1"); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json\n" + json.dumps({"ts": "x", "event": "intake", "label": "queue", "rule": "queue_marker"}) + "\n[1,2]\n")
    r = run(data_dir)
    assert r.returncode == 0 and "rule-decided: 1" in r.stdout and "Traceback" not in r.stderr


# --- M3 Task 5: hook latency percentiles and decision counts -------------------
#
# ``hookio.read_event`` stamps ``event["_hook_started"]``; every ``intake`` and
# ``dispatch`` log line carries ``hook_ms`` measured from that stamp (0 when
# the event was built by hand, as in these tests). The report prints p50/p95
# of ``hook_ms`` for intake and dispatch, split by whether a model call was
# made, and counts each ``decision`` value.

import io
import time

from qsc import dispatch, done, hookio, intake, store


def ev(prompt, prompt_id="pid-1", sid="s1"):   # copied from tests/test_intake.py
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt_id": prompt_id, "prompt": prompt, "transcript_path": None}


def judge_as(verdict, conf=0.9):   # copied from tests/test_dispatch.py
    return lambda *a, **k: done.Verdict(verdict, "r")


def rows(sid="s1"):
    return [json.loads(line) for line in paths.log_path(sid).read_text().splitlines()]


def test_report_latency_and_decisions(data_dir):
    log.record("s1", "intake", decision="new_turn", hook_ms=40)
    log.record("s1", "intake", label="queue", rule="queue_marker", hook_ms=90)
    log.record("s1", "intake", label="steer", classifier={"label": "steer"}, latency_ms=4000, hook_ms=4100)
    log.record("s1", "dispatch", hook_ms=30)
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "shadow_report.py")], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert "intake p50" in r.stdout and "dispatch p50" in r.stdout and "new_turn: 1" in r.stdout


def test_report_percentiles_split_by_model_call(data_dir):
    for ms in (10, 20, 30, 40, 100):
        log.record("s1", "intake", decision="new_turn", hook_ms=ms)
    log.record("s1", "intake", label="steer", classifier={"label": "steer"}, latency_ms=3000, hook_ms=3200)
    log.record("s1", "dispatch", next=None, hook_ms=5)
    log.record("s1", "dispatch", next=None, hook_ms=7)
    r = run(data_dir)
    assert r.returncode == 0
    # nearest-rank percentiles: 5 values -> p50 is the 3rd, p95 the 5th
    assert "intake p50 30 ms  p95 100 ms  (5 line(s), no model call)" in r.stdout
    assert "intake p50 3200 ms  p95 3200 ms  (1 line(s), model called" in r.stdout
    assert "classifier p50 3000 ms  p95 3000 ms" in r.stdout
    assert "dispatch p50 5 ms  p95 7 ms  (2 line(s), no model call)" in r.stdout
    assert "new_turn: 5" in r.stdout


def test_report_counts_every_decision_value(data_dir):
    for d in ("new_turn", "control", "dedupe", "approve", "revise", "defer_approval", "answer", "confirm", "parked_idle", "noop", "noop"):
        log.record("s1", "intake", decision=d, hook_ms=1)
    log.record("s1", "intake", label="steer", hook_ms=1)          # a mid-turn sort has no decision field
    log.record("s1", "dispatch", next=None, hook_ms=1)            # not an intake line: not counted
    r = run(data_dir)
    for d, n in (("new_turn", 1), ("control", 1), ("dedupe", 1), ("approve", 1), ("revise", 1), ("defer_approval", 1),
                 ("answer", 1), ("confirm", 1), ("parked_idle", 1), ("noop", 2), ("(none)", 1)):
        assert f"  {d}: {n}\n" in r.stdout, d


def test_report_latency_without_decisions_and_without_hook_ms(data_dir):
    # M0 to M2 lines have no hook_ms: they are left out of the percentiles, not counted as 0.
    log.record("s1", "intake", decision="new_turn")
    log.record("s1", "dispatch", next=None, hook_ms=3)
    r = run(data_dir)
    assert r.returncode == 0 and "no intake decisions" in r.stdout
    assert "intake p50 n/a" in r.stdout and "dispatch p50 3 ms  p95 3 ms" in r.stdout and "new_turn: 1" in r.stdout


def test_read_event_stamps_hook_start(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s1"})))
    before = time.monotonic()
    e = hookio.read_event("Stop")
    assert isinstance(e["_hook_started"], float) and before <= e["_hook_started"] <= time.monotonic()


def test_hook_ms_is_zero_without_a_stamp():
    hookio.start_clock({"hook_event_name": "Stop"})
    assert hookio.hook_ms() == 0
    hookio.start_clock({"_hook_started": "yesterday"})
    assert hookio.hook_ms() == 0
    hookio.start_clock({"_hook_started": time.monotonic() - 0.2})
    assert 190 <= hookio.hook_ms() < 5000


def test_intake_logs_hook_ms_from_the_stamp(data_dir):
    e = ev("add a file"); e["_hook_started"] = time.monotonic() - 0.25
    intake.handle(e)
    row = rows()[-1]
    assert row["event"] == "intake" and row["decision"] == "new_turn" and 240 <= row["hook_ms"] < 5000


def test_dispatch_logs_hook_ms_from_the_stamp(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("done"))
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid")
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done", "_hook_started": time.monotonic() - 0.25})
    dispatch_rows = [r for r in rows() if r["event"] == "dispatch"]
    assert dispatch_rows and all(240 <= r["hook_ms"] < 5000 for r in dispatch_rows)


def test_hand_built_events_log_hook_ms_zero(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("done"))
    intake.handle(ev("add a file"))
    with store.locked("s1") as s:
        s.new_task("b", ["p1"], "pid")
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"})
    logged = [r for r in rows() if r["event"] in ("intake", "dispatch")]
    assert {r["event"] for r in logged} == {"intake", "dispatch"}
    assert all(r["hook_ms"] == 0 for r in logged)
