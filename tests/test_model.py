import json
import time
from pathlib import Path

import pytest

from qsc import classify, model, procs

ROOT = Path(__file__).resolve().parent.parent
from conftest import FAKE_CLAUDE as FAKE, WINDOWS

posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX argv form; Windows uses windows_command")


# M2 Task 1 plan tests.

def test_call_returns_structured_output(monkeypatch, tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x")
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"verdict": "done"}}))
    assert model.call(p, {"type": "object"}, {"a": 1}, claude=FAKE) == {"verdict": "done"}


def test_call_none_on_missing_prompt(tmp_path):
    assert model.call(tmp_path / "nope.md", {}, {}, claude=FAKE) is None


@posix_only
def test_call_flags_and_model(monkeypatch, tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x"); seen = {}
    def fake_run(cmd, **kw): seen["cmd"] = cmd; raise FileNotFoundError
    monkeypatch.setattr(model.subprocess, "run", fake_run)
    model.call(p, {}, {}, model="sonnet")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet" and cmd[cmd.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in cmd and cmd[cmd.index("--tools") + 1] == ""


def test_classify_run_still_validates(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"label": "nope"}}))
    assert classify.run({"message": "x"}, claude=FAKE) is None


# Additional tests beyond the plan.

def test_flags_constant_in_order():
    assert model.FLAGS == ("--setting-sources", "", "--strict-mcp-config", "--tools", "")


@posix_only
def test_call_command_is_verbatim(monkeypatch, tmp_path):
    p = tmp_path / "sys.md"; p.write_text("the prompt"); seen = {}
    def fake_run(cmd, **kw): seen["cmd"] = cmd; seen["kw"] = kw; raise FileNotFoundError
    monkeypatch.setattr(model.subprocess, "run", fake_run)
    schema = {"type": "object", "properties": {"v": {"type": "string"}}}
    assert model.call(p, schema, {"k": "v"}, timeout=7.5, claude="/bin/claude-x") is None
    cmd = seen["cmd"]
    assert cmd[:3] == ["/bin/claude-x", "-p", "--model"] and cmd[3] == "haiku"
    assert tuple(cmd[4:9]) == model.FLAGS
    assert cmd[9:11] == ["--output-format", "json"]
    assert cmd[11] == "--json-schema" and json.loads(cmd[12]) == schema
    assert cmd[13] == "--system-prompt" and cmd[14] == "the prompt"
    assert json.loads(cmd[15]) == {"k": "v"} and len(cmd) == 16
    assert seen["kw"]["timeout"] == 7.5 and seen["kw"]["capture_output"] is True and seen["kw"]["text"] is True


def test_call_none_on_missing_binary(tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x")
    assert model.call(p, {}, {}, claude="/nonexistent/claude") is None


def test_call_none_on_timeout(monkeypatch, tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "3")
    t0 = time.time(); assert model.call(p, {}, {}, timeout=0.5, claude=FAKE) is None
    assert time.time() - t0 < 2


def test_call_none_on_non_zero_exit(tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x")
    if WINDOWS:
        fake = tmp_path / "claude.cmd"
        fake.write_text('@echo {"structured_output": {"a": 1}}\r\n@exit /b 1\r\n')
    else:
        fake = tmp_path / "claude"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' '{\"structured_output\": {\"a\": 1}}'\nexit 1\n")
        fake.chmod(0o755)
    assert model.call(p, {}, {}, claude=str(fake)) is None


def test_call_none_on_parse_error(monkeypatch, tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x")
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", "not json at all")
    assert model.call(p, {}, {}, claude=FAKE) is None


def test_call_none_on_non_dict_output(monkeypatch, tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x")
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps(["a", "b"]))
    assert model.call(p, {}, {}, claude=FAKE) is None
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": "done"}))
    assert model.call(p, {}, {}, claude=FAKE) is None
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": [1]}))
    assert model.call(p, {}, {}, claude=FAKE) is None
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"result": "done"}))
    assert model.call(p, {}, {}, claude=FAKE) is None


def test_call_none_on_unserialisable_payload(monkeypatch, tmp_path):
    p = tmp_path / "sys.md"; p.write_text("x")
    called = {}
    def fake_run(cmd, **kw): called["yes"] = True; raise FileNotFoundError
    monkeypatch.setattr(model.subprocess, "run", fake_run)
    assert model.call(p, {}, {"bad": object()}) is None and not called


def test_classify_run_delegates_to_model_call(monkeypatch):
    seen = {}
    def fake_call(prompt_path, schema, payload, timeout=15.0, claude="claude", model="haiku"):
        seen.update(prompt_path=prompt_path, schema=schema, payload=payload, timeout=timeout, claude=claude, model=model)
        return dict(label="queue", confidence=0.9, interrupt=False, parked=False, depends_on=["p1"], target=None, note=None, parts=[])
    monkeypatch.setattr(model, "call", fake_call)
    d = classify.run({"message": "x"}, timeout=3.0, claude="/bin/c")
    assert d == classify.Decision("queue", 0.9, ["p1"])
    assert seen["prompt_path"] == classify.PROMPT_PATH and seen["schema"] is classify.SCHEMA
    assert seen["payload"] == {"message": "x"} and seen["timeout"] == 3.0 and seen["claude"] == "/bin/c" and seen["model"] == "haiku"


def test_classify_run_none_when_model_call_none(monkeypatch):
    monkeypatch.setattr(model, "call", lambda *a, **k: None)
    assert classify.run({"message": "x"}) is None


# Windows support: the argv built for Windows, checked on every platform.

def test_windows_command_sends_the_prompt_by_file_and_no_payload(tmp_path):
    p = tmp_path / "sys.md"; p.write_text("the prompt")
    schema = json.dumps({"type": "object"})
    cmd = model.windows_command("/nonexistent/claude-x", "haiku", schema, p)
    assert cmd[:4] == ["/nonexistent/claude-x", "-p", "--model", "haiku"] and tuple(cmd[4:9]) == model.FLAGS
    assert cmd[9:13] == ["--output-format", "json", "--json-schema", schema]
    assert cmd[13:] == ["--system-prompt-file", str(p)]


def test_windows_command_resolves_claude_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(model.shutil, "which", lambda name: r"C:\npm\claude.CMD" if name == "claude" else None)
    cmd = model.windows_command("claude", "haiku", "{}", tmp_path / "sys.md")
    assert cmd[0] == r"C:\npm\claude.CMD"


def test_windows_command_refuses_cmd_metacharacters_through_a_batch_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(model.shutil, "which", lambda name: r"C:\npm\claude.cmd")
    for bad in ('{"a": "x & calc"}', '{"a": "%PATH%"}', '{"a": "1 | 2"}', '{"a": "a^b"}'):
        assert model.windows_command("claude", "haiku", bad, tmp_path / "sys.md") is None
    assert model.windows_command("claude", "haiku", "{}", tmp_path / "R&D" / "sys.md") is None
    monkeypatch.setattr(model.shutil, "which", lambda name: r"C:\bin\claude.exe")
    assert model.windows_command("claude", "haiku", '{"a": "x & y"}', tmp_path / "sys.md") is not None


def test_shipped_schemas_pass_through_a_batch_launcher():
    from qsc import done
    for schema in (classify.SCHEMA, done.JUDGE_SCHEMA):
        assert not model._CMD_META.search(json.dumps(schema))


@pytest.mark.skipif(not WINDOWS, reason="Windows process tree")
def test_windows_call_sends_the_payload_on_stdin(tmp_path, monkeypatch):
    import sys
    p = tmp_path / "sys.md"; p.write_text("x")
    echo = tmp_path / "echo.py"
    echo.write_text("import json, sys; print(json.dumps({'structured_output': json.loads(sys.stdin.read())}))\n")
    fake = tmp_path / "claude.cmd"
    fake.write_text(f'@"{sys.executable}" "{echo}"\r\n')
    payload = {"message": "caf\u00e9 \"quoted\" & <piped> | 100%"}
    assert model.call(p, {"type": "object"}, payload, claude=str(fake)) == payload
    assert procs.WINDOWS
