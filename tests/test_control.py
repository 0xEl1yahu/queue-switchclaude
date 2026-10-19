from qsc import control, store


def seeded():
    s = store.Store("s1"); a = s.new_task("add login", [], None); a.status = "running"; s.running_task = "p1"
    s.new_task("write tests for login", ["p1"], None); s.new_task("social login", ["p1"], None, status="parked"); return s


def test_list_shows_live_tasks():
    out = control.run(seeded(), "list")
    assert "p1" in out and "p2  queued" in out.replace("   ", "  ") and "p3  parked" in out.replace("   ", "  ")


def test_empty_command_is_list():
    assert control.run(store.Store("s1"), "") == "queue is empty"


def test_cancel_promote_park_messages():
    s = seeded()
    assert control.run(s, "cancel p2").startswith("cancelled p2") and s.get("p2").status == "cancelled"
    assert control.run(s, "cancel p9") == "no task p9"
    assert control.run(s, "promote p3") == "promoted p3 to the queue" and control.run(s, "promote p3") == "p3 is not parked"
    assert control.run(s, "park p3") == "parked p3" and control.run(s, "cancel p1") == "p1 is not queued, parked or paused"


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


def test_list_truncates_long_text_at_60():
    s = store.Store("s1"); s.new_task("x" * 100, [], None)
    line = control.run(s, "list")
    assert "x" * 60 in line and "x" * 61 not in line and line.endswith("…")


def test_commands_are_case_insensitive_and_whitespace_tolerant():
    s = seeded()
    assert control.run(s, "  CANCEL   p2  ") == "cancelled p2: write tests for login"
    assert control.run(s, "Help").splitlines() == control.run(s, "help").splitlines()


def test_cancel_promote_park_missing_or_wrong_status():
    s = seeded()
    assert control.run(s, "promote p9") == "no task p9"
    assert control.run(s, "park p9") == "no task p9"
    assert control.run(s, "park p1") == "p1 is not queued"
    assert control.run(s, "promote p2") == "p2 is not parked"
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
