import io
import json

from qsc import hookio, log, paths


def test_read_event_empty_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert hookio.read_event("UserPromptSubmit") is None


def test_read_event_wrong_event(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "Stop"}'))
    assert hookio.read_event("UserPromptSubmit") is None


def test_read_event_ok(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "Stop", "session_id": "s"}'))
    assert hookio.read_event("Stop")["session_id"] == "s"


def test_read_event_accepts_tuple(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "PostToolUse"}'))
    assert hookio.read_event(("PreToolUse", "PostToolUse"))["hook_event_name"] == "PostToolUse"


def test_shadow_flag(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW", "True"); assert hookio.shadow()
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SHADOW"); assert not hookio.shadow()


def test_log_record_appends_jsonl(data_dir):
    log.record("s1", "intake", label="queue"); log.record("s1", "intake", label="steer")
    lines = paths.log_path("s1").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["label"] == "steer"


# Additional cases beyond the plan's six.

def test_read_event_not_json(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hookio.read_event("Stop") is None


def test_read_event_not_a_dict(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('["Stop"]'))
    assert hookio.read_event("Stop") is None


def test_read_event_missing_event_name(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "s"}'))
    assert hookio.read_event("Stop") is None


def test_read_event_tuple_rejects_other(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "Stop"}'))
    assert hookio.read_event(("PreToolUse", "PostToolUse")) is None


def test_emit_writes_json(capsys):
    hookio.emit({"decision": "block", "reason": "r"})
    assert json.loads(capsys.readouterr().out) == {"decision": "block", "reason": "r"}


def test_emit_none_writes_nothing(capsys):
    hookio.emit(None)
    assert capsys.readouterr().out == ""


def test_emit_never_raises(capsys):
    hookio.emit({"bad": object()})  # not JSON-serialisable
    assert capsys.readouterr().out == ""


def test_shadow_flag_variants(monkeypatch):
    for v in ("1", "true", "YES", "Yes"):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW", v); assert hookio.shadow()
    for v in ("0", "false", "no", ""):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW", v); assert not hookio.shadow()


def test_log_record_has_ts_and_event(data_dir):
    log.record("s1", "dispatch", next=None, shadow=False)
    row = json.loads(paths.log_path("s1").read_text())
    assert row["event"] == "dispatch" and row["next"] is None and row["shadow"] is False
    assert row["ts"].endswith("+00:00") or row["ts"].endswith("Z")


def test_log_record_swallows_errors(data_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/dev/null/nope")
    log.record("s1", "intake", label="queue")  # must not raise
    log.record("s1", "intake", bad=object())   # not serialisable, must not raise
