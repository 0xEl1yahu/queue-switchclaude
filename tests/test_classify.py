import json
import time
from pathlib import Path

from qsc import classify

FAKE = str(Path(__file__).parent / "fakes" / "claude")


def test_run_parses_structured_output(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"label": "queue", "confidence": 0.95, "depends_on": ["p1"]}}))
    d = classify.run({"message": "x"}, claude=FAKE)
    assert d == classify.Decision("queue", 0.95, ["p1"])


def test_run_rejects_bad_label(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"label": "amend", "confidence": 0.9, "depends_on": []}}))
    assert classify.run({"message": "x"}, claude=FAKE) is None


def test_run_missing_binary_returns_none():
    assert classify.run({"message": "x"}, claude="/nonexistent/claude") is None


def test_run_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "3")
    t0 = time.time(); assert classify.run({"message": "x"}, timeout=0.5, claude=FAKE) is None
    assert time.time() - t0 < 2


def test_command_has_isolation_flags(monkeypatch):
    seen = {}
    def fake_run(cmd, **kw): seen["cmd"] = cmd; raise FileNotFoundError
    monkeypatch.setattr(classify.subprocess, "run", fake_run)
    classify.run({"message": "x"})
    cmd = seen["cmd"]
    for flag in ["--setting-sources", "--strict-mcp-config", "--tools", "--json-schema", "--system-prompt", "--output-format"]:
        assert flag in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku" and cmd[cmd.index("--setting-sources") + 1] == ""


# Additional tests beyond the plan (the plan's tests above are unchanged).

def test_run_rejects_confidence_out_of_range(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"label": "queue", "confidence": 1.5, "depends_on": []}}))
    assert classify.run({"message": "x"}, claude=FAKE) is None


def test_run_rejects_non_string_depends_on(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"label": "queue", "confidence": 0.9, "depends_on": [1]}}))
    assert classify.run({"message": "x"}, claude=FAKE) is None


def test_run_rejects_non_json_output(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", "not json at all")
    assert classify.run({"message": "x"}, claude=FAKE) is None


def test_run_rejects_missing_structured_output(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"result": "queue"}))
    assert classify.run({"message": "x"}, claude=FAKE) is None


def test_schema_shape():
    s = classify.SCHEMA
    assert s["type"] == "object" and s["additionalProperties"] is False
    assert set(s["required"]) == {"label", "confidence", "depends_on"}
    assert s["properties"]["label"]["enum"] == ["steer", "queue", "interrupt", "unclear"]
    assert s["properties"]["confidence"]["minimum"] == 0 and s["properties"]["confidence"]["maximum"] == 1
    assert s["properties"]["depends_on"]["items"]["type"] == "string"


def test_payload_shape():
    p = classify.payload("running", {"id": "p1", "text": "a"}, [{"id": "p2", "text": "b", "status": "queued", "depends_on": ["p1"]}], [{"role": "user", "text": "hi"}], "msg")
    assert p == {"state": "running", "running_task": {"id": "p1", "text": "a"},
                 "queue": [{"id": "p2", "text": "b", "status": "queued", "depends_on": ["p1"]}],
                 "recent": [{"role": "user", "text": "hi"}], "message": "msg"}
    assert json.dumps(p)


def test_command_passes_payload_json_last(monkeypatch):
    seen = {}
    def fake_run(cmd, **kw): seen["cmd"] = cmd; raise FileNotFoundError
    monkeypatch.setattr(classify.subprocess, "run", fake_run)
    classify.run({"message": "x", "state": "running"})
    cmd = seen["cmd"]
    assert json.loads(cmd[-1]) == {"message": "x", "state": "running"}
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == classify.SCHEMA
    assert "Answer with JSON only." in cmd[cmd.index("--system-prompt") + 1]
