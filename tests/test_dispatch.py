import json
from pathlib import Path
import subprocess
import sys

from qsc import dispatch, done, paths, store

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dispatch.py"


def test_dispatch_marks_done_and_hands_over_next(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "p2" in ctx and "b" in ctx and "Write a short plan for p2" in ctx
    s = store.load("s1")
    # v3: the next task is planned first, not executed; it stays queued until its plan is approved.
    assert s.get("p1").status == "done" and s.get("p2").status == "queued"
    assert s.state == "planning" and s.pending_task == "p2" and s.running_task is None


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
    plan = paths.plan_path("s1", "p1")
    assert out == {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext":
        "queue-switchclaude: next task is p1: write goodbye.py. Read the handoff of what came before at none. "
        f"Write a short plan for p1 to {plan} (goal, steps, how to verify), present it to the user in a few lines, "
        "then stop and wait for approval."}}
    s = store.load("s1"); assert s.state == "planning" and s.last_dispatched == "p1" and s.pending_task == "p1"


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
    s = store.load("s1")
    assert out is not None and s.state == "planning" and s.pending_task == "p1"


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
    # Claude wrote the plan; the second Stop records it and ends the turn.
    p = paths.plan_path("s1", "p1"); p.parent.mkdir(parents=True); p.write_text("# plan")
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": True})
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert [r["event"] for r in rows] == ["dispatch", "dispatch"]
    assert rows[0]["next"] == "p1" and rows[0]["shadow"] is False
    assert rows[1]["next"] is None and rows[1]["planned"] == "p1"


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
    s = store.load("s1"); assert s.state == "planning" and s.pending_task == "p1" and s.get("p1").status == "queued"


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


# --- M1 Task 7: pause-aware dispatch ----------------------------------------


def test_dispatch_respects_pause(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.paused = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").get("p1").status == "queued" and '"paused": true' in paths.log_path("s1").read_text()


def test_dispatch_keeps_paused_running_task(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "paused"; s.running_task = "p1"; s.state = "running"
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    assert store.load("s1").get("p1").status == "paused"


def test_dispatch_pause_still_closes_running_task(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid"); s.paused = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    s = store.load("s1")
    assert s.get("p1").status == "done" and s.state == "idle" and s.running_task is None
    assert s.get("p2").status == "queued" and s.paused is True


def test_dispatch_resume_dispatches_again(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.paused = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    with store.locked("s1") as s:
        s.paused = False
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    assert "p1" in out["hookSpecificOutput"]["additionalContext"] and store.load("s1").state == "planning"


def test_dispatch_shadow_respects_pause(data_dir):
    with store.locked("s1") as s:
        b = s.new_task("b", [], "pid"); b.status = "shadow_queued"; s.paused = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}, shadow=True) is None
    assert store.load("s1").get("p1").status == "shadow_queued"
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert rows[-1]["event"] == "dispatch" and rows[-1]["paused"] is True and rows[-1]["shadow"] is True


def test_dispatch_paused_running_task_clears_running_and_dispatches_independent(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "paused"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid"); s.new_task("c", [], "pid")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    s = store.load("s1")
    # p2 waits on the paused p1; p3 has no dependency and is planned next.
    assert "p3" in out["hookSpecificOutput"]["additionalContext"]
    assert s.get("p1").status == "paused" and s.get("p2").status == "queued"
    assert s.running_task is None and s.pending_task == "p3" and s.state == "planning"


def test_dispatch_handoff_includes_notes(data_dir):
    # An amend by subject appends a note to the queued task (intake._amend);
    # the handover must carry it or the note is lost (docs/m1-acceptance.md, H).
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        b = s.new_task("add a goodbye.py that prints goodbye", ["p1"], "pid")
        b.notes.append("the goodbye script should also print the date")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False})
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "next task is p2: add a goodbye.py that prints goodbye" in text
    assert "The user later added: the goodbye script should also print the date" in text
    assert text.index("goodbye.py") < text.index("print the date") < text.index("Read the handoff")


def test_dispatch_handoff_without_notes_is_unchanged(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False})
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "next task is p2: b. Read the handoff" in text and "later added" not in text


# --- M2 Task 5: plan-flow state machine --------------------------------------


def stop(sid="s1", closing="I finished it and ran the tests.", active=False, cwd=None):
    return {"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": closing, "stop_hook_active": active, "cwd": cwd or str(ROOT)}


def judge_as(verdict, conf=0.9):
    return lambda *a, **k: done.Verdict(verdict, "r")


def test_running_done_writes_handoff_and_asks_for_plan(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("done"))
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"; s.new_task("b", ["p1"], "pid")
    out = dispatch.handle(stop())
    s = store.load("s1")
    assert s.get("p1").status == "done" and paths.handoff_path("s1", "p1").exists() and "I finished it" in paths.handoff_path("s1", "p1").read_text()
    assert s.state == "planning" and s.pending_task == "p2"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Write a short plan for p2" in ctx and str(paths.plan_path("s1", "p2")) in ctx and str(paths.handoff_path("s1", "p1")) in ctx


def test_running_verify_failed_hands_back_twice_then_gives_up(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; a.verify = "exit 2"; s.running_task = "p1"; s.state = "running"
    assert "failed its verify command" in dispatch.handle(stop())["hookSpecificOutput"]["additionalContext"]
    assert "failed its verify command" in dispatch.handle(stop(active=True))["hookSpecificOutput"]["additionalContext"]
    assert dispatch.handle(stop(active=True)) is None
    assert store.load("s1").get("p1").status == "done" and store.load("s1").get("p1").verdict == "gave_up"


def test_running_asked_question_waits_for_answer(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("asked_question"))
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
    assert dispatch.handle(stop(closing="Which DB?")) is None
    s = store.load("s1"); assert s.state == "awaiting_answer" and s.pending_task == "p1" and s.get("p1").status == "running"


def test_planning_with_plan_file_awaits_approval(data_dir):
    with store.locked("s1") as s:
        b = s.new_task("b", [], "pid"); s.state = "planning"; s.pending_task = "p1"
    p = paths.plan_path("s1", "p1"); p.parent.mkdir(parents=True); p.write_text("# plan")
    assert dispatch.handle(stop(active=True)) is None
    s = store.load("s1"); assert s.state == "awaiting_approval" and s.get("p1").status == "planned" and s.get("p1").plan_based_on == []


def test_planning_without_plan_file_retries_then_requeues(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.state = "planning"; s.pending_task = "p1"
    assert "Write a short plan for p1" in dispatch.handle(stop(active=True))["hookSpecificOutput"]["additionalContext"]
    assert dispatch.handle(stop(active=True)) is None
    s = store.load("s1"); assert s.state == "idle" and s.get("p1").status == "queued" and s.pending_task is None      # review focus 2


def test_planning_auto_task_executes_immediately(data_dir):
    with store.locked("s1") as s:
        b = s.new_task("b", [], "pid"); b.auto = True; s.state = "planning"; s.pending_task = "p1"
    p = paths.plan_path("s1", "p1"); p.parent.mkdir(parents=True); p.write_text("# plan")
    out = dispatch.handle(stop(active=True))
    assert "Execute the plan at" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p1"                                     # review focus 5


def test_idle_stop_represents_planned_task(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("done"))
    with store.locked("s1") as s:
        b = s.new_task("b", [], "pid"); b.status = "planned"; b.plan_based_on = []
        c = s.new_task("unrelated", [], "pid"); c.status = "running"; s.running_task = "p2"; s.state = "running"
    out = dispatch.handle(stop())
    assert "waiting for your approval" in out["hookSpecificOutput"]["additionalContext"] and store.load("s1").state == "awaiting_approval"   # review focus 1


def test_stale_plan_is_requeued(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("done"))
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        b = s.new_task("b", ["p1"], "pid"); b.status = "planned"; b.plan_based_on = []
    p = paths.plan_path("s1", "p2"); p.parent.mkdir(parents=True); p.write_text("# old plan")
    out = dispatch.handle(stop())
    s = store.load("s1"); assert s.get("p2").status == "queued" and not p.exists() and "Write a short plan for p2" in out["hookSpecificOutput"]["additionalContext"]


def test_hooks_json_stop_timeout():
    h = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    assert h["Stop"][0]["hooks"][0]["timeout"] == 180


# --- extra coverage for the stage functions ----------------------------------


def test_running_not_done_hands_back_then_closes(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("not_done"))
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
    ctx = dispatch.handle(stop(closing="did half"))["hookSpecificOutput"]["additionalContext"]
    assert ctx == "queue-switchclaude: task p1 does not look finished: r. Finish it, then stop."
    s = store.load("s1"); assert s.get("p1").status == "running" and s.get("p1").attempts == 1 and s.state == "running"
    assert "does not look finished" in dispatch.handle(stop(closing="did more", active=True))["hookSpecificOutput"]["additionalContext"]
    assert dispatch.handle(stop(closing="still not", active=True)) is None
    t = store.load("s1").get("p1")
    assert t.status == "done" and t.verdict == "gave_up" and t.closing == "still not"
    assert "gave_up" in paths.handoff_path("s1", "p1").read_text()


def test_verify_failed_handback_carries_output_tail(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; a.verify = "echo boom; exit 3"; s.running_task = "p1"; s.state = "running"
    ctx = dispatch.handle(stop())["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("queue-switchclaude: task p1 failed its verify command (exit 3). Output tail: boom")
    assert ctx.endswith("Fix it, then stop.")


def test_verify_ok_closes_with_verdict(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; a.verify = "true"; s.running_task = "p1"; s.state = "running"
    assert dispatch.handle(stop()) is None
    t = store.load("s1").get("p1")
    assert t.status == "done" and t.verdict == "verify_ok" and t.closing == "I finished it and ran the tests."
    text = paths.handoff_path("s1", "p1").read_text()
    assert text.startswith("# Handoff p1\n\n## Task\na\n") and "## Verdict\nverify_ok: exit 0" in text and text.rstrip().endswith("I finished it and ran the tests.")


def test_write_handoff_lists_notes_and_marks_done(data_dir):
    with store.locked("s1") as s:
        t = s.new_task("add login", [], "pid"); t.notes = ["use jwt", "no sessions"]; t.status = "running"
        path = dispatch.write_handoff(s, t, done.Verdict("confirmed", "user confirmed"), closing="done, merged")
    assert path == paths.handoff_path("s1", "p1") and path.exists()
    text = path.read_text()
    assert "## Notes\n- use jwt\n- no sessions\n" in text and "## Verdict\nconfirmed: user confirmed" in text and "done, merged" in text
    t = store.load("s1").get("p1"); assert t.status == "done" and t.verdict == "confirmed" and t.closing == "done, merged"


def test_judge_sees_plan_excerpt(data_dir, monkeypatch):
    seen = {}
    def fake_judge(task_text, notes, plan_excerpt, closing, claude="claude"):
        seen.update(task=task_text, plan=plan_excerpt, closing=closing); return done.Verdict("done", "r")
    monkeypatch.setattr(done, "judge", fake_judge)
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
    p = paths.plan_path("s1", "p1"); p.parent.mkdir(parents=True); p.write_text("# plan\n" + "x" * 3000)
    dispatch.handle(stop(closing="all done"))
    assert seen["task"] == "a" and seen["closing"] == "all done" and seen["plan"].startswith("# plan") and len(seen["plan"]) == 1500


def test_asked_question_keeps_the_question_as_closing(data_dir, monkeypatch):
    monkeypatch.setattr(done, "judge", judge_as("asked_question"))
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
    dispatch.handle(stop(closing="Which DB?"))
    s = store.load("s1"); assert s.running_task is None and s.get("p1").closing == "Which DB?"


def test_stop_while_waiting_changes_nothing(data_dir):
    for state in ("awaiting_approval", "awaiting_answer"):
        with store.locked("s1") as s:
            s.tasks = []; s.next_id = 1
            b = s.new_task("b", [], "pid"); b.status = "planned" if state == "awaiting_approval" else "running"
            s.state = state; s.pending_task = "p1"; s.running_task = None
        assert dispatch.handle(stop(active=True)) is None
        s = store.load("s1"); assert s.state == state and s.pending_task == "p1"


def test_planning_handover_auto_wording_and_handoffs(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "done"
        b = s.new_task("b", [], "pid"); b.status = "done"
        c = s.new_task("c", ["p1", "p2"], "pid"); c.auto = True
    h1 = paths.handoff_path("s1", "p1"); h1.parent.mkdir(parents=True); h1.write_text("# Handoff p1")
    ctx = dispatch.handle(stop())["hookSpecificOutput"]["additionalContext"]
    # Only the handoff that exists is named; the missing one is not invented.
    assert f"Read the handoff of what came before at {h1}. " in ctx and str(paths.handoff_path("s1", "p2")) not in ctx
    assert ctx.endswith("then carry it out immediately without waiting.")


def test_choose_next_returns_text_without_touching_awaiting_approval(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
        text = dispatch.choose_next(s)
        assert text.startswith("queue-switchclaude: next task is p1: b.") and s.state == "planning" and s.pending_task == "p1"
    with store.locked("s1") as s:
        s.state = "awaiting_approval"; s.get("p1").status = "planned"
        assert dispatch.choose_next(s) is None and s.state == "awaiting_approval" and s.pending_task == "p1"


def test_start_and_handback_shapes(data_dir):
    with store.locked("s1") as s:
        t = s.new_task("b", [], "pid"); t.status = "planned"; s.state = "awaiting_approval"; s.pending_task = "p1"
        text = dispatch.start(s, t)
        assert text == f"queue-switchclaude: task p1 is approved. Execute the plan at {paths.plan_path('s1', 'p1')} now. When it is done, say what was produced and stop."
        assert t.status == "running" and s.running_task == "p1" and s.state == "running" and s.pending_task is None
    assert dispatch.handback("x") == {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "x"}}


def test_planned_task_with_unfinished_dependency_is_not_represented(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid")
        b = s.new_task("b", ["p1"], "pid"); b.status = "planned"; b.plan_based_on = []
    ctx = dispatch.handle(stop())["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1")
    assert "Write a short plan for p1" in ctx and s.get("p2").status == "planned" and s.pending_task == "p1"


def test_planning_stop_never_resurrects_a_cancelled_or_parked_task(data_dir):
    # `queue: cancel p1` (or `park p1`) typed while Claude was writing p1's plan: the
    # plan file still appears, but the task must keep its status, never become
    # `planned`, and dispatch must move on to the next ready task.
    for status in ("cancelled", "parked"):
        with store.locked("s1") as s:
            s.tasks = []; s.next_id = 1; s.last_dispatched = None
            a = s.new_task("a", [], "pid"); s.new_task("b", [], "pid")
            s.state = "planning"; s.pending_task = "p1"; a.status = status
        p = paths.plan_path("s1", "p1"); p.parent.mkdir(parents=True, exist_ok=True); p.write_text("# plan")
        out = dispatch.handle(stop(active=True))
        s = store.load("s1")
        assert s.get("p1").status == status and s.state == "planning" and s.pending_task == "p2"
        assert "Write a short plan for p2" in out["hookSpecificOutput"]["additionalContext"]
        rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
        assert any(r["event"] == "plan_failed" and r["task"] == "p1" and status in r["reason"] for r in rows)


def test_plan_failed_is_logged(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.state = "planning"; s.pending_task = "p1"
    dispatch.handle(stop(active=True)); dispatch.handle(stop(active=True))
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert rows[-1]["event"] == "plan_failed" and rows[-1]["task"] == "p1"


def test_shadow_closes_running_task_without_handback(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; a.verify = "exit 2"; s.running_task = "p1"; s.state = "running"
    assert dispatch.handle(stop(), shadow=True) is None
    t = store.load("s1").get("p1")
    assert t.status == "done" and t.verdict == "verify_failed" and paths.handoff_path("s1", "p1").exists()
