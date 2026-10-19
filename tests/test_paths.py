from qsc import paths


def test_data_dir_uses_env(data_dir):
    assert paths.data_dir() == data_dir
    assert paths.store_path("s1") == data_dir / "sessions" / "s1.json"
    assert paths.log_path("s1") == data_dir / "log" / "s1.jsonl"


def test_data_dir_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setattr(paths, "PLUGIN_DATA_ROOT", tmp_path / "no-plugins")   # no installed data dir
    assert paths.data_dir().name == "queue-switchclaude"


def test_session_id_cannot_escape_data_dir(data_dir):
    p = paths.store_path("../evil")
    assert p.parent == data_dir / "sessions" and p.name == "___evil.json"
    assert paths.log_path("a b/c").name == "a_b_c.jsonl"


# M2 Task 2: per-session directory for plans and handoffs. Spec §4.3 wrote
# `plans/pN.md`; they live under the plugin data dir, not the user's
# repository, so the working tree stays clean.

def test_plan_and_handoff_paths(data_dir):
    assert paths.plan_path("s1", "p2") == data_dir / "sessions" / "s1" / "plans" / "p2.md"
    assert paths.handoff_path("s1", "p2") == data_dir / "sessions" / "s1" / "handoffs" / "p2.md"
    assert paths.plan_path("../x", "p1").parent.parent.name == "___x"


def test_session_dir(data_dir):
    assert paths.session_dir("s1") == data_dir / "sessions" / "s1"
    assert paths.session_dir("a b/c") == data_dir / "sessions" / "a_b_c"


def test_task_id_cannot_escape_session_dir(data_dir):
    p = paths.plan_path("s1", "../../evil")
    assert p.parent == data_dir / "sessions" / "s1" / "plans" and p.name == "______evil.md"
    h = paths.handoff_path("s1", "a/b")
    assert h.parent == data_dir / "sessions" / "s1" / "handoffs" and h.name == "a_b.md"


def test_data_dir_falls_back_to_installed_plugin_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    root = tmp_path / "plugins" / "data"
    old = root / "queue-switchclaude-old"; new = root / "queue-switchclaude-inline"; other = root / "other-plugin"
    for d in (old, new, other):
        d.mkdir(parents=True)
    (old / "last_session").write_text("a"); (new / "last_session").write_text("b")
    import os, time
    os.utime(old / "last_session", (1, 1))
    monkeypatch.setattr(paths, "PLUGIN_DATA_ROOT", root)
    assert paths.data_dir() == new


def test_data_dir_env_wins_and_last_resort(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "x"))
    assert paths.data_dir() == tmp_path / "x"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.setattr(paths, "PLUGIN_DATA_ROOT", tmp_path / "nope")
    assert paths.data_dir().name == "queue-switchclaude"
