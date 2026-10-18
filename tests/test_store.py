import json

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
