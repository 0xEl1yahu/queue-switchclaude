import json
from datetime import datetime, timezone

import pytest

from qsc import paths, store


def test_load_missing_returns_fresh(data_dir):
    s = store.load("s1")
    assert s.state == "idle" and s.tasks == [] and s.next_id == 1


def test_save_then_load_roundtrip(data_dir):
    s = store.load("s1"); t = s.new_task("add login", [], "pid-1"); store.save(s)
    s2 = store.load("s1")
    assert s2.tasks[0].id == "p1" and s2.tasks[0].text == "add login" and s2.next_id == 2


def test_corrupt_file_is_fresh(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True); p.write_text("{not json")
    assert store.load("s1").tasks == []


def test_ready_requires_all_deps_done(data_dir):
    s = store.load("s1")
    a = s.new_task("a", [], None); b = s.new_task("b", ["p1"], None); c = s.new_task("c", ["p1", "p2"], None)
    assert [t.id for t in s.ready()] == ["p1"]
    a.status = "done"
    assert [t.id for t in s.ready()] == ["p2"]
    b.status = "done"
    assert [t.id for t in s.ready()] == ["p3"]


def test_save_is_atomic_no_partial_file(data_dir):
    s = store.load("s1"); store.save(s)
    assert not list(paths.store_path("s1").parent.glob("*.tmp*"))


# Additional tests beyond the plan's five.

def test_new_task_fields_and_ids_never_reused(data_dir):
    s = store.load("s1")
    t = s.new_task("x", ["p0"], "pid")
    assert t.id == "p1" and t.status == "queued" and t.depends_on == ["p0"] and t.prompt_id == "pid"
    assert t.created_at.endswith("Z") or "+00:00" in t.created_at
    assert s.get("p1") is t and s.get("p9") is None
    store.save(s)
    s2 = store.load("s1")
    assert s2.new_task("y", [], None).id == "p2"


def test_truncated_file_is_fresh(data_dir):
    s = store.load("s1"); s.new_task("a", [], None); store.save(s)
    p = paths.store_path("s1")
    p.write_text(p.read_text()[:20])
    assert store.load("s1").tasks == []


def test_wrong_shape_is_fresh(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"session_id": "s1", "tasks": [{"id": "p1"}]}))
    assert store.load("s1").tasks == []
    p.write_text(json.dumps([1, 2, 3]))
    assert store.load("s1").tasks == []


def test_locked_saves_on_exit(data_dir):
    with store.locked("s1") as s:
        s.new_task("a", [], "pid")
        s.state = "running"
    s2 = store.load("s1")
    assert s2.state == "running" and s2.tasks[0].id == "p1"
    assert not list(paths.store_path("s1").parent.glob("*.tmp*"))


def test_locked_does_not_save_on_exception(data_dir):
    s0 = store.load("s1"); s0.new_task("keep", [], None); store.save(s0)
    try:
        with store.locked("s1") as s:
            s.new_task("lost", [], None)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert [t.text for t in store.load("s1").tasks] == ["keep"]


def test_ready_ignores_non_queued_and_unknown_deps(data_dir):
    s = store.load("s1")
    a = s.new_task("a", [], None); a.status = "running"
    s.new_task("b", ["p1"], None)
    s.new_task("c", ["nope"], None)
    d = s.new_task("d", [], None); d.status = "dropped"
    s.new_task("e", [], None)
    assert [t.id for t in s.ready()] == ["p5"]


def test_save_creates_parent_directories(data_dir):
    store.save(store.load("deep"))
    assert paths.store_path("deep").exists()
    assert json.loads(paths.store_path("deep").read_text())["session_id"] == "deep"


# Regression: plan review focus 4 says a corrupt store is treated as fresh
# *and the corruption logged*; load() used to reset silently.

def _log_rows(session_id):
    return [json.loads(line) for line in paths.log_path(session_id).read_text().splitlines()]


def test_corrupt_file_is_logged(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True); p.write_text("{not json")
    assert store.load("s1").tasks == []
    row = _log_rows("s1")[-1]
    assert row["event"] == "store_corrupt" and row["path"] == str(p)
    assert row["error"].startswith("JSONDecodeError")


def test_wrong_shape_is_logged(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"session_id": "s1", "tasks": [{"id": "p1"}]}))
    assert store.load("s1").tasks == []
    assert _log_rows("s1")[-1]["event"] == "store_corrupt"


def test_missing_file_is_not_logged(data_dir):
    assert store.load("s1").tasks == []
    assert not paths.log_path("s1").exists()


# M1 Task 1: store v2 (statuses, normalised text, find_recent, cancel/promote,
# forward-compatible load).

def test_new_task_fills_norm_text_and_status(data_dir):
    s = store.load("s1"); t = s.new_task("  Add a /Login endpoint!  ", [], None, status="parked")
    assert t.norm_text == "add a /login endpoint" and t.status == "parked"


def test_find_recent_window_and_statuses(data_dir):
    s = store.load("s1"); t = s.new_task("add login", [], None)
    assert s.find_recent("add login", 600).id == "p1"
    t.created_at = "2020-01-01T00:00:00Z"
    assert s.find_recent("add login", 600) is None
    u = s.new_task("add login", [], None); u.status = "cancelled"
    assert s.find_recent("add login", 600) is None


def test_cancel_and_promote_rules(data_dir):
    s = store.load("s1"); a = s.new_task("a", [], None, status="parked"); b = s.new_task("b", [], None); b.status = "done"
    assert s.promote("p1").status == "queued" and s.promote("p1") is None
    assert s.cancel("p1").status == "cancelled" and s.cancel("p2") is None and s.cancel("p9") is None


def test_unknown_keys_are_ignored_not_corrupt(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"state": "idle", "future_field": 1, "next_id": 2, "tasks": [{"id": "p1", "text": "x", "status": "queued", "depends_on": [], "prompt_id": None, "created_at": "2026-10-19T00:00:00Z", "future": True}]}))
    s = store.load("s1"); assert s.get("p1").text == "x" and "store_corrupt" not in (paths.log_path("s1").read_text() if paths.log_path("s1").exists() else "")


def test_m0_store_file_loads(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"session_id": "s1", "state": "idle", "current_prompt_id": None, "running_task": None, "next_id": 3, "interrupt": False, "last_dispatched": "p2", "tasks": [{"id": "p1", "text": "a", "status": "done", "depends_on": [], "prompt_id": "x", "created_at": "2026-10-18T21:59:04Z"}]}))
    assert store.load("s1").get("p1").norm_text == "" and store.load("s1").next_id == 3


# Additional Task 1 tests beyond the plan's five.

def test_task_statuses_extended():
    for status in ("queued", "parked", "running", "paused", "done", "interrupted",
                   "dropped", "cancelled", "shadow_queued", "shadow_dispatched"):
        assert status in store.TASK_STATUSES


@pytest.mark.parametrize("raw,expected", [
    ("  Add a /Login endpoint!  ", "add a /login endpoint"),
    ("Don’t   do\tthat.", "don't do that"),
    ("“quoted”", '"quoted"'),
    ("trailing?!.", "trailing"),
    ("", ""),
    ("...", ""),
])
def test_normalise(raw, expected):
    assert store.normalise(raw) == expected


def test_new_task_default_status_is_queued(data_dir):
    t = store.load("s1").new_task("x", [], None)
    assert t.status == "queued" and t.notes == []


def test_find_recent_prefers_newest_and_skips_dropped(data_dir):
    s = store.load("s1")
    s.new_task("add login", [], None)
    s.new_task("add login", [], None)
    assert s.find_recent("add login", 600).id == "p2"
    s.get("p2").status = "dropped"
    assert s.find_recent("add login", 600).id == "p1"
    assert s.find_recent("something else", 600) is None


def test_find_recent_with_explicit_now(data_dir):
    s = store.load("s1"); t = s.new_task("add login", [], None)
    t.created_at = "2026-10-19T00:00:00Z"
    assert s.find_recent("add login", 600, now=datetime(2026, 10, 19, 0, 5, tzinfo=timezone.utc)).id == "p1"
    assert s.find_recent("add login", 600, now=datetime(2026, 10, 19, 0, 11, tzinfo=timezone.utc)) is None


def test_find_recent_ignores_unparseable_created_at(data_dir):
    s = store.load("s1"); t = s.new_task("add login", [], None)
    t.created_at = "not a date"
    assert s.find_recent("add login", 600) is None


def test_cancel_covers_paused_and_promote_only_parked(data_dir):
    s = store.load("s1")
    a = s.new_task("a", [], None); a.status = "paused"
    b = s.new_task("b", [], None); b.status = "running"
    assert s.cancel("p1").status == "cancelled" and s.cancel("p2") is None
    assert s.promote("p2") is None and s.promote("p9") is None


def test_candidates_order_and_statuses(data_dir):
    s = store.load("s1")
    a = s.new_task("a", [], None); a.status = "running"
    s.new_task("b", [], None)
    s.new_task("c", [], None, status="parked")
    d = s.new_task("d", [], None); d.status = "paused"
    e = s.new_task("e", [], None); e.status = "done"
    f = s.new_task("f", [], None); f.status = "cancelled"
    g = s.new_task("g", [], None); g.status = "shadow_queued"
    assert [t.id for t in s.candidates()] == ["p1", "p2", "p3", "p4"]


def test_ready_excludes_parked_and_paused(data_dir):
    s = store.load("s1")
    s.new_task("a", [], None, status="parked")
    b = s.new_task("b", [], None); b.status = "paused"
    s.new_task("c", [], None)
    assert [t.id for t in s.ready()] == ["p3"]


def test_v2_fields_roundtrip(data_dir):
    s = store.load("s1"); t = s.new_task("Add login", [], None, status="parked"); t.notes.append("use argon2")
    store.save(s)
    t2 = store.load("s1").get("p1")
    assert t2.norm_text == "add login" and t2.status == "parked" and t2.notes == ["use argon2"]


def test_wrong_shape_still_corrupt_after_key_filtering(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"tasks": [{"id": "p1", "future": True}]}))
    assert store.load("s1").tasks == []
    assert _log_rows("s1")[-1]["event"] == "store_corrupt"
