import json
from pathlib import Path
import subprocess
import sys

from qsc import interrupt, paths, store

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "interrupt.py"


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
    interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    rows =[json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
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
    r = subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


def test_script_ignores_other_events(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
    assert store.load("s1").interrupt is True


def test_script_emits_stop_on_flag(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1"}), capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["continue"] is False


def test_interrupt_drops_queued_tasks_and_says_so(data_dir):
    with store.locked("s1") as s:
        t = s.new_task("x", [], "pid"); t.status = "running"; s.running_task = "p1"; s.state = "running"; s.interrupt = True
        s.new_task("write tests", ["p1"], "pid")
    out = interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert out["continue"] is False and "dropped queued tasks: p2 (write tests)" in out["stopReason"]
    s = store.load("s1"); assert s.get("p2").status == "dropped" and s.ready() == []


def test_interrupt_clears_pending_task_and_keeps_planned(data_dir):
    # 2026-10-20 review fix 3: a plan waiting for approval is not thrown away
    # by a stop aimed at the turn that is running; Esc never dropped it either.
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"; s.interrupt = True
        b = s.new_task("b", ["p1"], "pid"); b.status = "planned"; s.pending_task = "p2"
    out = interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert out["continue"] is False and "p2" not in out["stopReason"]
    s = store.load("s1"); assert s.pending_task is None and s.get("p2").status == "planned" and s.state == "idle"
    # It no longer waits on the interrupted p1, so dispatch can present it again.
    assert s.get("p2").depends_on == []


# --- M3 Task 4: the hook's cost ----------------------------------------------

def test_interrupt_skips_ask_user_question(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    assert interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "AskUserQuestion"}) is None
    assert store.load("s1").interrupt is True


def test_interrupt_skips_ask_user_question_without_reading_the_store(data_dir, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("store read")
    monkeypatch.setattr(store, "load", boom)
    assert interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "AskUserQuestion"}) is None


def test_interrupt_still_stops_other_tools_with_tool_name(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    out = interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash"})
    assert out["continue"] is False and store.load("s1").interrupt is False


# --- M4 Task 3: PreToolUse only -----------------------------------------------

def test_interrupt_ignores_post_tool_use_now(data_dir):
    with store.locked("s1") as s: s.interrupt = True
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"hook_event_name": "PostToolUse", "session_id": "s1"}), capture_output=True, text=True)
    assert r.stdout == "" and store.load("s1").interrupt is True


# Additional tests beyond the plan's one.

def test_handle_returns_none_for_post_tool_use_without_touching_the_store(data_dir, monkeypatch):
    with store.locked("s1") as s:
        t = s.new_task("x", [], "pid"); t.status = "running"; s.running_task = "p1"; s.state = "running"; s.interrupt = True
    def boom(*a, **k):
        raise AssertionError("store read")
    original_load = store.load
    monkeypatch.setattr(store, "load", boom)
    assert interrupt.handle({"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Bash"}) is None
    monkeypatch.setattr(store, "load", original_load)
    s = store.load("s1"); assert s.interrupt is True and s.get("p1").status == "running"
    assert not paths.log_path("s1").exists()


def test_handle_requires_the_pre_tool_use_name(data_dir):
    with store.locked("s1") as s: s.interrupt = True
    assert interrupt.handle({"session_id": "s1"}) is None
    assert interrupt.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").interrupt is True
    assert interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})["continue"] is False


# --- 2026-10-20 review fix 3: stop and Esc share one reset -------------------

from qsc import intake  # noqa: E402


def _seed_running(s):
    """p1 done, p2 running, p3 queued after p2, p4 planned (deferred) after p1,
    p5 parked after p2."""
    s.new_task("a", [], "pid-1").status = "done"
    b = s.new_task("b", ["p1"], "pid-1"); b.status = "running"; s.running_task = "p2"; s.state = "running"
    s.current_prompt_id = "pid-1"
    s.new_task("c", ["p2"], "pid-1")
    s.new_task("d", ["p1"], "pid-1").status = "planned"
    s.new_task("e", ["p2"], "pid-1", status="parked")


def _seed_planning(s):
    """p1 done, p2 being planned (queued, pending), p3 queued after p2."""
    s.new_task("a", [], "pid-1").status = "done"
    s.new_task("b", ["p1"], "pid-1"); s.state = "planning"; s.pending_task = "p2"; s.current_prompt_id = "pid-1"
    s.new_task("c", ["p2"], "pid-1")


def _summary(s):
    return [(t.id, t.status, list(t.depends_on)) for t in s.tasks], s.state, s.running_task, s.pending_task, s.interrupt


def _after_stop(seed, sid):
    with store.locked(sid) as s:
        seed(s); s.interrupt = True
    interrupt.handle({"hook_event_name": "PreToolUse", "session_id": sid})
    return store.load(sid)


def _after_esc(seed, sid):
    with store.locked(sid) as s:
        seed(s)
    with store.locked(sid) as s:
        intake._reset_turn(s)
    return store.load(sid)


def test_stop_and_esc_reset_a_running_turn_the_same_way(data_dir):
    stopped, escaped = _after_stop(_seed_running, "s1"), _after_esc(_seed_running, "s2")
    assert _summary(stopped) == _summary(escaped)
    assert stopped.get("p2").status == "interrupted" and stopped.get("p3").status == "dropped"
    assert stopped.get("p4").status == "planned" and stopped.get("p5").status == "parked"
    assert stopped.get("p5").depends_on == ["p1"]                         # rewired past the interrupted p2


def test_stop_and_esc_reset_a_planning_turn_the_same_way(data_dir):
    for sid in ("s1", "s2"):
        plan = paths.plan_path(sid, "p2"); plan.parent.mkdir(parents=True, exist_ok=True); plan.write_text("half a plan")
    stopped, escaped = _after_stop(_seed_planning, "s1"), _after_esc(_seed_planning, "s2")
    assert _summary(stopped) == _summary(escaped)
    assert stopped.get("p2").status == "queued" and stopped.get("p3").status == "queued"
    assert stopped.state == "idle" and stopped.pending_task is None
    assert not paths.plan_path("s1", "p2").exists() and not paths.plan_path("s2", "p2").exists()


def test_stop_during_planning_says_the_task_is_queued_again(data_dir):
    with store.locked("s1") as s:
        _seed_planning(s); s.interrupt = True
    out = interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert out["continue"] is False and "p2 is queued again" in out["stopReason"] and "dropped" not in out["stopReason"]


def test_stop_keeps_a_planned_tasks_plan_file(data_dir):
    plan = paths.plan_path("s1", "p4"); plan.parent.mkdir(parents=True, exist_ok=True); plan.write_text("the plan")
    stopped = _after_stop(_seed_running, "s1")
    assert stopped.get("p4").status == "planned" and plan.read_text() == "the plan"


def test_stop_leaves_a_paused_running_task_paused(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "paused"; s.running_task = "p1"; s.state = "running"; s.interrupt = True
    interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert store.load("s1").get("p1").status == "paused"
