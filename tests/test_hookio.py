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


# --- M4 Task 2: honest hook latency ------------------------------------------
#
# Each entry script stamps ``time.monotonic()`` as its first statement and
# hands it to ``read_event(expected, started=_T0)``, so ``hook_ms`` covers the
# ``qsc`` imports as well as the work. A missing or future stamp reads 0.

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_read_event_takes_a_start_stamp(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "Stop"}'))
    e = hookio.read_event("Stop", started=123.0)
    assert e["_hook_started"] == 123.0


def test_hook_ms_never_negative():
    assert hookio.hook_ms({"_hook_started": 10**12}) == 0 and hookio.hook_ms({}) == 0     # review focus 2


def test_entry_scripts_stamp_before_imports():
    for name in ("intake", "dispatch", "interrupt", "queue_cli"):
        src = (ROOT / "scripts" / f"{name}.py").read_text().lstrip()
        body = src.split('"""', 2)[-1] if src.startswith('"""') else src          # skip the module docstring
        first = next(l for l in body.splitlines() if l.strip() and not l.startswith("#") and not l.startswith("from __future__"))
        assert first.startswith("import time; _T0 = time.monotonic()"), name


# Additional cases beyond the plan's three.

def test_read_event_without_a_start_stamp_uses_the_parse_clock(monkeypatch):
    import time
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "Stop"}'))
    before = time.monotonic()
    e = hookio.read_event("Stop")
    assert before <= e["_hook_started"] <= time.monotonic()


def test_hook_ms_from_an_event_stamp():
    import time
    assert 190 <= hookio.hook_ms({"_hook_started": time.monotonic() - 0.2}) < 5000
    assert hookio.hook_ms({"_hook_started": "yesterday"}) == 0
    assert hookio.hook_ms({"_hook_started": True}) == 0


def test_hook_ms_without_an_event_uses_the_remembered_clock():
    import time
    hookio.start_clock({"_hook_started": time.monotonic() - 0.2})
    assert 190 <= hookio.hook_ms() < 5000
    hookio.start_clock({})
    assert hookio.hook_ms() == 0


def test_entry_scripts_pass_the_stamp_to_read_event():
    for name in ("intake", "dispatch", "interrupt"):
        src = (ROOT / "scripts" / f"{name}.py").read_text()
        assert "started=_T0" in src, name
