from qsc import paths


def test_data_dir_uses_env(data_dir):
    assert paths.data_dir() == data_dir
    assert paths.store_path("s1") == data_dir / "sessions" / "s1.json"
    assert paths.log_path("s1") == data_dir / "log" / "s1.jsonl"


def test_data_dir_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert paths.data_dir().name == "queue-switchclaude"
