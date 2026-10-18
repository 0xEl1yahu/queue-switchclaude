import json
import subprocess
import sys

from qsc import interrupt, paths, store


def test_interrupt_flag_stops_and_resets(data_dir):
    with store.locked("s1") as s:
        t = s.new_task("x", [], "pid"); t.status = "running"; s.running_task = "p1"; s.state = "running"; s.interrupt = True
    out = interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert out == {"continue": False, "stopReason": "queue-switchclaude: stopped as requested"}
    s = store.load("s1"); assert s.interrupt is False and s.state == "idle" and s.get("p1").status == "interrupted"


def test_interrupt_no_flag_passes(data_dir):
    assert interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"}) is None


def test_interrupt_logs_event(data_dir):
    with store.locked("s1") as s:
        t = s.new_task("x", [], "pid"); t.status = "running"; s.running_task = "p1"; s.state = "running"; s.interrupt = True
    interrupt.handle({"hook_event_name": "PostToolUse", "session_id": "s1"})
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert rows[-1]["event"] == "interrupt" and rows[-1]["task"] == "p1"


def test_interrupt_no_flag_leaves_store_untouched(data_dir):
    interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert not paths.store_path("s1").exists()


def test_interrupt_flag_with_no_running_task_still_stops(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    out = interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert out["continue"] is False
    s = store.load("s1"); assert s.interrupt is False and s.state == "idle" and s.running_task is None


def test_script_swallows_bad_stdin():
    r = subprocess.run([sys.executable, "scripts/interrupt.py"], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


def test_script_ignores_other_events(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    r = subprocess.run([sys.executable, "scripts/interrupt.py"], input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
    assert store.load("s1").interrupt is True


def test_script_emits_stop_on_flag(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    r = subprocess.run([sys.executable, "scripts/interrupt.py"], input=json.dumps({"hook_event_name": "PostToolUse", "session_id": "s1"}), capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["continue"] is False
