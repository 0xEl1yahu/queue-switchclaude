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
