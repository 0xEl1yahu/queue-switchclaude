"""M3 Task 2: session garbage collection (``qsc.gc``) and its one call per
session from dispatch."""
import os
import time
from pathlib import Path

from qsc import dispatch, gc, intake, log, paths, store

ROOT = Path(__file__).resolve().parent.parent


def ev(prompt, prompt_id="pid-1", sid="s1"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt_id": prompt_id, "prompt": prompt, "transcript_path": None}


def stop(sid="s1", closing="I finished it and ran the tests.", active=False, cwd=None):
    return {"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": closing, "stop_hook_active": active, "cwd": cwd or str(ROOT)}


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def seed(sid, age_days, plan=False):
    s = store.load(sid); store.save(s); log.record(sid, "x")
    files = [paths.store_path(sid), paths.log_path(sid)]
    if plan:
        p = paths.plan_path(sid, "p1"); p.parent.mkdir(parents=True, exist_ok=True); p.write_text("# plan")
        files.append(p)
    for p in files:
        age(p, age_days)
    for d in (paths.session_dir(sid), paths.session_dir(sid) / "plans"):
        if d.exists():
            age(d, age_days)


def test_gc_deletes_old_keeps_young_and_protected(data_dir):
    for sid, age_days in (("old", 40), ("young", 3), ("me", 400)):
        s = store.load(sid); store.save(s); log.record(sid, "x")
        for p in (paths.store_path(sid), paths.log_path(sid)):
            t = time.time() - age_days * 86400; os.utime(p, (t, t))
    deleted = gc.collect(keep_days=30, protect={"me"})
    assert deleted == ["old"] and paths.store_path("young").exists() and paths.store_path("me").exists()   # review focus 4
    assert not paths.store_path("old").exists() and not paths.log_path("old").exists()
    assert paths.log_path("young").exists() and paths.log_path("me").exists()


def test_gc_runs_once_per_session_from_dispatch(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(gc, "collect", lambda **kw: (calls.append(kw), [])[1])
    intake.handle(ev("a")); dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"})
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"})
    assert len(calls) == 1 and calls[0]["protect"] == {"s1"} and store.load("s1").gc_at


def test_gc_removes_the_session_directory_and_lock(data_dir):
    seed("old", 40, plan=True)
    assert paths.store_path("old").with_name("old.json.lock").exists()
    assert gc.collect(keep_days=30) == ["old"]
    assert not paths.session_dir("old").exists()
    assert not paths.store_path("old").with_name("old.json.lock").exists()


def test_gc_newest_file_decides(data_dir):
    # An old store whose session directory holds a fresh plan is still in use.
    seed("busy", 40, plan=True)
    fresh = paths.plan_path("busy", "p1"); fresh.write_text("# plan"); age(fresh, 1)
    assert gc.collect(keep_days=30) == []
    assert paths.store_path("busy").exists()


def test_gc_future_mtime_and_window_edge_are_kept(data_dir):
    now = time.time()
    for sid, age_days in (("future", -1), ("edge", 30), ("just-over", 30.001)):
        seed(sid, 0)
        for p in (paths.store_path(sid), paths.log_path(sid)):
            t = now - age_days * 86400; os.utime(p, (t, t))
    # "future": clock skew, a file from tomorrow is young; "edge": exactly at
    # the window is not older than it; only "just-over" goes.
    assert gc.collect(keep_days=30, now=now) == ["just-over"]
    assert paths.store_path("future").exists() and paths.store_path("edge").exists()


def test_gc_protect_wins_over_age(data_dir):
    seed("a", 400); seed("b", 400)
    assert gc.collect(keep_days=30, protect={"a"}) == ["b"]
    assert paths.store_path("a").exists() and paths.log_path("a").exists()


def test_gc_protect_matches_unsafe_session_ids(data_dir):
    seed("a b/c", 400)
    assert gc.collect(keep_days=30, protect={"a b/c"}) == []
    assert paths.store_path("a b/c").exists()
    assert gc.collect(keep_days=30) == ["a_b_c"]


def test_gc_drops_pointers_naming_deleted_sessions(data_dir):
    seed("old", 40); seed("live", 1)
    intake_pointer = paths.pointer_path(); intake_pointer.parent.mkdir(parents=True, exist_ok=True); intake_pointer.write_text("old\n")
    cp_old = paths.cwd_pointer_path("/proj/old"); cp_old.parent.mkdir(parents=True, exist_ok=True); cp_old.write_text("old\n")
    cp_live = paths.cwd_pointer_path("/proj/live"); cp_live.write_text("live\n")
    assert gc.collect(keep_days=30) == ["old"]
    assert not intake_pointer.exists() and not cp_old.exists() and cp_live.read_text().strip() == "live"


def test_gc_on_empty_data_dir(data_dir):
    assert gc.collect() == []
    assert gc.collect(keep_days=0, now=time.time() + 10) == []


def test_dispatch_gc_errors_are_swallowed(data_dir, monkeypatch):
    def boom(**kw):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(gc, "collect", boom)
    intake.handle(ev("a"))
    assert dispatch.handle(stop()) is None
    s = store.load("s1")
    assert s.gc_at and s.get("p1").status == "done"


def test_dispatch_gc_never_deletes_the_running_session(data_dir):
    seed("s1", 400)
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
    for p in (paths.store_path("s1"), paths.log_path("s1")):
        age(p, 400)
    out = dispatch.handle(stop())
    assert out is not None and "Write a short plan for p1" in out["hookSpecificOutput"]["additionalContext"]
    assert paths.store_path("s1").exists() and store.load("s1").get("p1").status == "queued"


def test_gc_ages_out_an_orphan_lock(data_dir):
    lock = paths.store_path("orphan").with_name("orphan.json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True); lock.write_text(""); age(lock, 40)
    fresh = paths.store_path("kept").with_name("kept.json.lock"); fresh.write_text("")
    assert gc.collect(keep_days=30) == ["orphan"]
    assert not lock.exists() and fresh.exists()


def test_store_gc_at_defaults_and_roundtrips(data_dir):
    s = store.load("s1")
    assert s.gc_at is None
    s.gc_at = "2026-10-19T00:00:00Z"; store.save(s)
    assert store.load("s1").gc_at == "2026-10-19T00:00:00Z"
