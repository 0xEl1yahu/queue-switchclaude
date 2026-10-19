import json
from pathlib import Path
import subprocess
import sys

from qsc import dispatch, paths, store

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dispatch.py"


def test_dispatch_marks_done_and_hands_over_next(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False})
    assert "p2" in out["hookSpecificOutput"]["additionalContext"] and "b" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.get("p1").status == "done" and s.get("p2").status == "running" and s.running_task == "p2"


def test_dispatch_nothing_ready_goes_idle(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").state == "idle"


def test_dispatch_spin_guard(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.last_dispatched = "p1"
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": True}) is None


def test_dispatch_shadow_does_not_change_status(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}, shadow=True) is None
    assert store.load("s1").get("p1").status == "queued"


def test_dispatch_after_interrupt_does_not_dispatch(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.interrupt = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").interrupt is False


def test_dispatch_output_shape(data_dir):
    with store.locked("s1") as s:
        s.new_task("write goodbye.py", [], "pid")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    assert out == {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext":
        "queue-switchclaude: the user queued task p1 to run after the previous work. Do it now: write goodbye.py"}}
    s = store.load("s1"); assert s.state == "running" and s.last_dispatched == "p1"


def test_dispatch_keeps_current_prompt_id(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid-1"); a.status = "running"; s.running_task = "p1"; s.state = "running"; s.current_prompt_id = "pid-1"
        s.new_task("b", ["p1"], "pid-1")
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    assert store.load("s1").current_prompt_id == "pid-1"


def test_dispatch_spin_guard_only_when_stop_hook_active(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.last_dispatched = "p1"
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False})
    assert out is not None and store.load("s1").get("p1").status == "running"


def test_dispatch_dependency_not_done_is_not_dispatched(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid"); s.new_task("c", ["p2"], "pid")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    assert "p2" in out["hookSpecificOutput"]["additionalContext"]
    assert store.load("s1").get("p3").status == "queued"


def test_dispatch_logs(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert [r["event"] for r in rows] == ["dispatch", "dispatch"]
    assert rows[0]["next"] == "p1" and rows[0]["shadow"] is False and rows[1]["next"] is None


def test_dispatch_after_interrupt_resets_running_task_and_drops_queue(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid"); s.interrupt = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    s = store.load("s1")
    # D5 applied to an explicit stop: the queued dependant is dropped, not stranded.
    assert s.get("p1").status == "interrupted" and s.state == "idle" and s.running_task is None and s.get("p2").status == "dropped"


def test_script_swallows_bad_stdin():
    r = subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


def test_script_ignores_other_events(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"}), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
    assert store.load("s1").get("p1").status == "queued"


def test_script_dispatches_next(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}), capture_output=True, text=True)
    assert r.returncode == 0 and "p1" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert store.load("s1").get("p1").status == "running"


def test_script_shadow_env_does_not_dispatch(data_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW", "true")
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
    assert store.load("s1").get("p1").status == "queued"
    assert '"shadow": true' in paths.log_path("s1").read_text()


def test_shadow_dispatch_reports_would_be_task(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        b = s.new_task("b", ["p1"], "pid"); b.status = "shadow_queued"
        c = s.new_task("c", ["p2"], "pid"); c.status = "shadow_queued"
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}, shadow=True) is None
    s = store.load("s1")
    assert s.get("p1").status == "done" and s.get("p2").status == "shadow_dispatched" and s.get("p3").status == "shadow_queued"
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}, shadow=True) is None
    assert store.load("s1").get("p3").status == "shadow_dispatched"
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines() if '"dispatch"' in l]
    assert [r["next"] for r in rows] == ["p2", "p3"] and all(r["shadow"] for r in rows)


def test_shadow_queued_tasks_never_dispatch_for_real(data_dir):
    with store.locked("s1") as s:
        b = s.new_task("b", [], "pid"); b.status = "shadow_queued"
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").get("p1").status == "shadow_queued"
