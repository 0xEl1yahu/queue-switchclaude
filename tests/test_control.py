from qsc import control, rules, store


def seeded():
    s = store.Store("s1"); a = s.new_task("add login", [], None); a.status = "running"; s.running_task = "p1"
    s.new_task("write tests for login", ["p1"], None); s.new_task("social login", ["p1"], None, status="parked"); return s


def test_list_shows_live_tasks():
    out = control.run(seeded(), "list")
    assert "\n" not in out and "p1 running: add login" in out and "p2 queued after p1: write tests for login" in out and "p3 parked after p1: social login" in out


def test_empty_command_is_list():
    assert control.run(store.Store("s1"), "") == "queue is empty"


def test_cancel_promote_park_messages():
    s = seeded()
    assert control.run(s, "cancel p2").startswith("cancelled p2") and s.get("p2").status == "cancelled"
    assert control.run(s, "cancel p9") == "no task p9"
    assert control.run(s, "promote p3") == "promoted p3 to the queue" and control.run(s, "promote p3") == "p3 is not parked, paused or failed_verify"
    assert control.run(s, "park p3") == "parked p3" and control.run(s, "cancel p1") == "p1 is not queued, parked, paused, planned or failed_verify"


def test_drop_and_pause():
    s = seeded()
    assert control.run(s, "drop") == "dropped 2 tasks: p2, p3" and all(t.status == "dropped" for t in s.tasks[1:])
    assert control.run(s, "pause") == "queue paused" and s.paused and control.run(s, "resume") == "queue resumed" and not s.paused


def test_unknown_shows_help():
    assert "cancel pN" in control.run(store.Store("s1"), "frobnicate")


# Additional tests beyond the plan's five.

def test_list_hides_finished_tasks_and_shows_deps():
    s = seeded()
    d = s.new_task("old", [], None); d.status = "done"
    e = s.new_task("gone", [], None); e.status = "cancelled"
    out = control.run(s, "list")
    assert "p4" not in out and "p5" not in out
    assert "after p1" in out and out.index("p1") < out.index("p2") < out.index("p3")


def test_list_truncates_long_text_per_item():
    s = store.Store("s1"); s.new_task("x" * 100, [], None)
    line = control.run(s, "list")
    assert "x" * control.ITEM_WIDTH in line and "x" * (control.ITEM_WIDTH + 1) not in line and line.endswith("…")


def test_commands_are_case_insensitive_and_whitespace_tolerant():
    s = seeded()
    assert control.run(s, "  CANCEL   p2  ") == "cancelled p2: write tests for login"
    assert control.run(s, "Help").splitlines() == control.run(s, "help").splitlines()


def test_cancel_promote_park_missing_or_wrong_status():
    s = seeded()
    assert control.run(s, "promote p9") == "no task p9"
    assert control.run(s, "park p9") == "no task p9"
    assert control.run(s, "park p1") == "p1 is not queued"
    assert control.run(s, "promote p2") == "p2 is not parked, paused or failed_verify"
    assert control.run(s, "cancel") != "" and "cancel pN" in control.run(s, "cancel")


def test_cancel_paused_task():
    s = seeded(); s.get("p2").status = "paused"
    assert control.run(s, "cancel p2") == "cancelled p2: write tests for login"


def test_drop_leaves_running_and_paused_alone_and_reports_empty():
    s = seeded(); s.get("p2").status = "paused"
    assert control.run(s, "drop") == "dropped 1 task: p3"
    assert s.get("p1").status == "running" and s.get("p2").status == "paused"
    assert control.run(s, "drop") == "dropped 0 tasks"


def test_paused_persists_through_save_and_load(data_dir):
    with store.locked("s1") as s:
        assert s.paused is False
        control.run(s, "pause")
    assert store.load("s1").paused is True


def test_help_lists_every_command():
    out = control.run(store.Store("s1"), "help")
    for word in ("list", "cancel pN", "promote pN", "park pN", "drop", "pause", "resume"):
        assert word in out


def test_resume_pn_and_promote_bring_a_paused_task_back():
    s = seeded(); s.get("p2").status = "paused"
    assert control.run(s, "resume p2") == "promoted p2 to the queue" and s.get("p2").status == "queued"
    s.get("p2").status = "paused"
    assert control.run(s, "promote p2") == "promoted p2 to the queue"
    assert control.run(s, "resume") == "queue resumed"


def test_help_and_list_are_single_lines():
    s = seeded(); s.paused = True
    assert "\n" not in control.run(s, "help") and "resume [pN]" in control.run(s, "help")
    out = control.run(s, "list")
    assert "\n" not in out and out.endswith("queue is paused, send 'queue: resume' to dispatch again")


# --- M2 Task 3: verify, auto, manual, show, approve ---------------------------

from qsc import paths  # noqa: E402


def test_verify_auto_manual_show(data_dir):
    s = seeded()
    assert control.run(s, "verify p2 pytest -q tests/test_login.py") == "p2 verifies with: pytest -q tests/test_login.py"
    assert s.get("p2").verify == "pytest -q tests/test_login.py"
    assert control.run(s, "auto p2") == "p2 will run without approval" and s.get("p2").auto
    assert control.run(s, "manual p2") == "p2 will wait for approval" and not s.get("p2").auto
    out = control.run(s, "show p2", session_id="s1")
    assert out.startswith("p2 queued: write tests for login") and "verify: pytest" in out and "plan:" not in out


def test_approve_command(data_dir):
    s = seeded(); s.state = "awaiting_approval"; s.pending_task = "p2"; s.get("p2").status = "planned"
    assert control.run(s, "approve") == "approved p2, it runs now" and s.state == "running" and s.running_task == "p2" and s.get("p2").status == "running"
    assert control.run(s, "approve") == "nothing is waiting for approval"


# Additional tests beyond the plan's two.

def test_show_names_plan_and_handoff_files_when_they_exist(data_dir):
    s = seeded()
    plan = paths.plan_path("s1", "p2"); plan.parent.mkdir(parents=True); plan.write_text("# plan")
    handoff = paths.handoff_path("s1", "p2"); handoff.parent.mkdir(parents=True); handoff.write_text("# handoff")
    out = control.run(s, "show p2", session_id="s1")
    assert "\n" not in out
    assert out.startswith("p2 queued: write tests for login")
    assert f"; plan: {plan}" in out and f"; handoff: {handoff}" in out and "verify:" not in out
    # Another session's files are not this session's.
    assert "plan:" not in control.run(s, "show p2", session_id="s2")


def test_show_without_session_id_omits_file_paths(data_dir):
    s = seeded()
    plan = paths.plan_path("s1", "p2"); plan.parent.mkdir(parents=True); plan.write_text("# plan")
    assert control.run(s, "show p2") == "p2 queued: write tests for login"


def test_show_missing_task_and_usage():
    s = seeded()
    assert control.run(s, "show p9") == "no task p9"
    assert control.run(s, "show") == "usage: show pN"
    assert control.run(s, "SHOW 2").startswith("p2 queued: write tests for login")


def test_show_marks_auto_and_is_one_line():
    s = seeded(); s.get("p1").text = "add login\n  with   two   factors"
    control.run(s, "auto p1")
    out = control.run(s, "show p1")
    assert out == "p1 running: add login with two factors; auto"


def test_verify_keeps_the_rest_of_the_line_verbatim():
    s = seeded()
    cmd = 'python3 -c "import sys; sys.exit(1)"  # trailing'
    assert control.run(s, f"verify p2 {cmd}") == f"p2 verifies with: {cmd}"
    assert s.get("p2").verify == cmd
    assert control.run(s, "verify 2 true") == "p2 verifies with: true" and s.get("p2").verify == "true"


def test_verify_usage_and_missing_task():
    s = seeded()
    assert control.run(s, "verify") == "usage: verify pN <command>"
    assert control.run(s, "verify p2") == "usage: verify pN <command>"
    assert control.run(s, "verify p9 true") == "no task p9"
    assert s.get("p2").verify is None


def test_verify_auto_manual_apply_to_live_or_planned_tasks_only():
    s = seeded()
    s.get("p2").status = "planned"
    assert control.run(s, "verify p2 true") == "p2 verifies with: true"
    assert control.run(s, "verify p1 true") == "p1 verifies with: true"          # running is live
    d = s.new_task("old", [], None); d.status = "done"
    assert control.run(s, "verify p4 true") == "p4 is done, not live" and d.verify is None
    assert control.run(s, "auto p4") == "p4 is done, not live" and not d.auto
    assert control.run(s, "manual p4") == "p4 is done, not live"
    assert control.run(s, "auto p9") == "no task p9" and control.run(s, "manual p9") == "no task p9"
    assert control.run(s, "auto") == "usage: auto pN" and control.run(s, "manual") == "usage: manual pN"


def test_list_marks_the_pending_planned_task():
    s = seeded(); s.get("p2").status = "planned"
    assert "p2 planned after p1: write tests for login" in control.run(s, "list")
    s.state = "awaiting_approval"; s.pending_task = "p2"
    out = control.run(s, "list")
    assert "p2 planned (awaiting approval) after p1: write tests for login" in out and "\n" not in out
    # A different planned task is not the one waiting.
    s.get("p3").status = "planned"
    assert "p3 planned after p1: social login" in control.run(s, "list")


def test_approve_clears_pending_and_ignores_other_states():
    s = seeded(); s.get("p1").status = "done"; s.running_task = None
    s.state = "awaiting_answer"; s.pending_task = "p2"
    assert control.run(s, "approve") == "nothing is waiting for approval" and s.state == "awaiting_answer"
    s.state = "awaiting_approval"; s.get("p2").status = "planned"
    assert control.run(s, "approve") == "approved p2, it runs now"
    assert s.pending_task is None and s.running_task == "p2" and s.state == "running"


def test_approve_with_an_id_must_name_the_pending_task():
    s = seeded(); s.state = "awaiting_approval"; s.pending_task = "p2"; s.get("p2").status = "planned"
    assert control.run(s, "approve p3") == "p3 is not waiting for approval; p2 is"
    assert s.state == "awaiting_approval" and s.get("p2").status == "planned"
    assert control.run(s, "approve p2") == "approved p2, it runs now"


def test_help_lists_the_plan_flow_commands():
    out = control.run(store.Store("s1"), "help")
    assert "\n" not in out
    for word in ("show pN", "verify pN <command>", "auto pN", "manual pN", "approve"):
        assert word in out
    for word in ("verify", "auto", "manual", "show", "approve"):
        assert word in control.COMMANDS


# --- M3 Task 3: reply strings name every status; shadow tasks are editable ----

def test_control_replies_name_every_status():
    s = seeded(); s.get("p2").status = "done"
    assert control.run(s, "cancel p2") == "p2 is not queued, parked, paused, planned or failed_verify"
    assert control.run(s, "promote p2") == "p2 is not parked, paused or failed_verify"


def test_shadow_control_edits_shadow_tasks():
    s = seeded(); s.get("p2").status = "shadow_queued"
    assert control.run(s, "cancel p2").startswith("cancelled p2")


# Additional tests beyond the plan's two.

def test_shadow_park_and_drop_act_on_shadow_tasks():
    s = seeded(); s.get("p2").status = "shadow_queued"
    assert control.run(s, "park p2") == "parked p2" and s.get("p2").status == "parked"
    s.get("p2").status = "shadow_queued"
    assert control.run(s, "drop") == "dropped 2 tasks: p2, p3"
    assert s.get("p2").status == "dropped" and s.get("p3").status == "dropped"


def test_cancel_planned_and_failed_verify_tasks():
    s = seeded(); s.get("p2").status = "planned"
    assert control.run(s, "cancel p2") == "cancelled p2: write tests for login"
    s.get("p3").status = "failed_verify"
    assert control.run(s, "cancel p3") == "cancelled p3: social login"


def test_promote_failed_verify_task():
    s = seeded(); s.get("p2").status = "failed_verify"
    assert control.run(s, "promote p2") == "promoted p2 to the queue" and s.get("p2").status == "queued"


# --- M4 Task 3: the help line names every control word -------------------------

def test_help_names_every_control_word():
    text = control.help_text()
    for w in rules.CONTROL_WORDS:
        assert w in text, w


# Additional tests beyond the plan's one.

def test_help_names_every_control_word_as_a_whole_word_on_one_line():
    import re
    text = control.help_text()
    assert "\n" not in text and text == control.run(store.Store("s1"), "help")
    for w in rules.CONTROL_WORDS:
        assert re.search(rf"(^|[ |]){w}( |$)", text), w
    assert "resume [pN]" in text and "show pN" in text
    assert set(rules.CONTROL_WORDS) == set(control.COMMANDS)
