"""M3 Task 2: session garbage collection (``qsc.gc``) and its one call per
session from dispatch."""
import json
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
    deleted = gc.collect(keep_days=30, protect={"me"})[0]
    assert deleted == ["old"] and paths.store_path("young").exists() and paths.store_path("me").exists()   # review focus 4
    assert not paths.store_path("old").exists() and not paths.log_path("old").exists()
    assert paths.log_path("young").exists() and paths.log_path("me").exists()


def test_gc_runs_once_per_session_from_dispatch(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(gc, "collect", lambda **kw: (calls.append(kw), ([], []))[1])
    intake.handle(ev("a")); dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"})
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"})
    assert len(calls) == 1 and calls[0]["protect"] == {"s1"} and store.load("s1").gc_at


def test_gc_removes_the_session_directory_and_lock(data_dir):
    seed("old", 40, plan=True)
    assert paths.store_path("old").with_name("old.json.lock").exists()
    assert gc.collect(keep_days=30)[0] == ["old"]
    assert not paths.session_dir("old").exists()
    assert not paths.store_path("old").with_name("old.json.lock").exists()


def test_gc_newest_file_decides(data_dir):
    # An old store whose session directory holds a fresh plan is still in use.
    seed("busy", 40, plan=True)
    fresh = paths.plan_path("busy", "p1"); fresh.write_text("# plan"); age(fresh, 1)
    assert gc.collect(keep_days=30)[0] == []
    assert paths.store_path("busy").exists()


def test_gc_future_mtime_and_window_edge_are_kept(data_dir):
    now = time.time()
    for sid, age_days in (("future", -1), ("edge", 30), ("just-over", 30.001)):
        seed(sid, 0)
        for p in (paths.store_path(sid), paths.log_path(sid)):
            t = now - age_days * 86400; os.utime(p, (t, t))
    # "future": clock skew, a file from tomorrow is young; "edge": exactly at
    # the window is not older than it; only "just-over" goes.
    assert gc.collect(keep_days=30, now=now)[0] == ["just-over"]
    assert paths.store_path("future").exists() and paths.store_path("edge").exists()


def test_gc_protect_wins_over_age(data_dir):
    seed("a", 400); seed("b", 400)
    assert gc.collect(keep_days=30, protect={"a"})[0] == ["b"]
    assert paths.store_path("a").exists() and paths.log_path("a").exists()


def test_gc_protect_matches_unsafe_session_ids(data_dir):
    seed("a b/c", 400)
    assert gc.collect(keep_days=30, protect={"a b/c"})[0] == []
    assert paths.store_path("a b/c").exists()
    assert gc.collect(keep_days=30)[0] == ["a_b_c"]


def test_gc_drops_pointers_naming_deleted_sessions(data_dir):
    seed("old", 40); seed("live", 1)
    intake_pointer = paths.pointer_path(); intake_pointer.parent.mkdir(parents=True, exist_ok=True); intake_pointer.write_text("old\n")
    cp_old = paths.cwd_pointer_path("/proj/old"); cp_old.parent.mkdir(parents=True, exist_ok=True); cp_old.write_text("old\n")
    cp_live = paths.cwd_pointer_path("/proj/live"); cp_live.write_text("live\n")
    deleted_sessions, deleted_pointers = gc.collect(keep_days=30)
    assert deleted_sessions == ["old"] and deleted_pointers == [cp_old.name]
    assert not intake_pointer.exists() and not cp_old.exists() and cp_live.read_text().strip() == "live"


def test_gc_on_empty_data_dir(data_dir):
    assert gc.collect() == ([], [])
    assert gc.collect(keep_days=0, now=time.time() + 10) == ([], [])


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
    assert gc.collect(keep_days=30)[0] == ["orphan"]
    assert not lock.exists() and fresh.exists()


def test_store_gc_at_defaults_and_roundtrips(data_dir):
    s = store.load("s1")
    assert s.gc_at is None
    s.gc_at = "2026-10-19T00:00:00Z"; store.save(s)
    assert store.load("s1").gc_at == "2026-10-19T00:00:00Z"


# --- M4 Task 3: stale pointers ----------------------------------------------

def test_gc_removes_stale_pointers_but_not_live_ones(data_dir):
    intake.handle({**ev("a", sid="live"), "cwd": "/p/live"})                      # live: store exists
    (paths.data_dir() / "pointers").mkdir(exist_ok=True)
    (paths.data_dir() / "pointers" / "deadbeef").write_text("gone\n")              # names a session with no files
    intake.handle({**ev("ok", sid="fresh"), "cwd": "/p/fresh"})                   # pass-through: pointer, no store yet
    deleted_sessions, deleted_pointers = gc.collect(keep_days=30, protect={"fresh"})
    assert deleted_pointers == ["deadbeef"] and deleted_sessions == []                       # review focus 3
    assert paths.cwd_pointer_path("/p/fresh").exists() and paths.cwd_pointer_path("/p/live").exists()


# Additional tests beyond the plan's one.

def test_gc_returns_a_tuple_of_two_sorted_lists(data_dir):
    seed("old-b", 40); seed("old-a", 40)
    pointers = paths.pointers_dir(); pointers.mkdir(parents=True, exist_ok=True)
    (pointers / "zz").write_text("nobody\n"); (pointers / "aa").write_text("old-b\n")
    out = gc.collect(keep_days=30)
    assert isinstance(out, tuple) and len(out) == 2
    assert out == (["old-a", "old-b"], ["aa", "zz"])
    assert not (pointers / "zz").exists() and not (pointers / "aa").exists()


def test_gc_keeps_pointers_of_sessions_with_any_file(data_dir):
    # A log alone, a session directory alone or a lock alone still says the
    # session exists; only a pointer naming nothing at all is stale.
    log.record("logged", "x")
    paths.session_dir("dir-only").mkdir(parents=True)
    lock = paths.store_path("locked").with_name("locked.json.lock"); lock.write_text("")
    pointers = paths.pointers_dir(); pointers.mkdir(parents=True, exist_ok=True)
    for name, sid in (("p1", "logged"), ("p2", "dir-only"), ("p3", "locked"), ("p4", "gone")):
        (pointers / name).write_text(sid + "\n")
    assert gc.collect(keep_days=30) == ([], ["p4"])
    assert all((pointers / n).exists() for n in ("p1", "p2", "p3"))


def test_gc_stale_pointer_check_honours_protect_and_safe_ids(data_dir):
    pointers = paths.pointers_dir(); pointers.mkdir(parents=True, exist_ok=True)
    (pointers / "mine").write_text("a b/c\n")           # the running session, store not written yet
    (pointers / "other").write_text("x y/z\n")
    assert gc.collect(keep_days=30, protect={"a b/c"}) == ([], ["other"])
    assert (pointers / "mine").exists()
    seed("q r", 1)                                        # stored as q_r
    (pointers / "raw").write_text("q r\n")
    assert gc.collect(keep_days=30, protect={"a b/c"}) == ([], [])
    assert (pointers / "raw").exists()


def test_gc_leaves_temp_and_dot_files_and_the_global_pointer_alone(data_dir):
    pointers = paths.pointers_dir(); pointers.mkdir(parents=True, exist_ok=True)
    (pointers / ".abc.tmp").write_text("gone\n")
    (pointers / ".hidden").write_text("gone\n")
    sub = pointers / "subdir"; sub.mkdir()
    glob = paths.pointer_path(); glob.write_text("gone\n")
    assert gc.collect(keep_days=30) == ([], [])
    assert (pointers / ".abc.tmp").exists() and (pointers / ".hidden").exists() and sub.exists()
    assert glob.read_text().strip() == "gone"


def test_gc_empty_pointer_is_stale(data_dir):
    pointers = paths.pointers_dir(); pointers.mkdir(parents=True, exist_ok=True)
    (pointers / "blank").write_text("")
    (pointers / "spaces").write_text("  \n")
    assert gc.collect(keep_days=30) == ([], ["blank", "spaces"])


def test_gc_stale_pointers_without_a_pointers_dir(data_dir):
    assert not paths.pointers_dir().exists()
    assert gc.collect(keep_days=30) == ([], [])


def test_dispatch_logs_stale_pointer_cleanup(data_dir):
    pointers = paths.pointers_dir(); pointers.mkdir(parents=True, exist_ok=True)
    (pointers / "stale").write_text("gone\n")
    intake.handle(ev("a")); dispatch.handle(stop())
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    row = next(r for r in rows if r["event"] == "gc")
    assert row["deleted"] == [] and row["pointers"] == ["stale"]
    assert not (pointers / "stale").exists()


def test_dispatch_writes_no_gc_line_when_nothing_was_removed(data_dir):
    intake.handle(ev("a")); dispatch.handle(stop())
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert not any(r["event"] == "gc" for r in rows) and store.load("s1").gc_at
