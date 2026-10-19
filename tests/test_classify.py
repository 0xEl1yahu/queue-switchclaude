import dataclasses
import json
import time
from pathlib import Path

from qsc import classify

FAKE = str(Path(__file__).parent / "fakes" / "claude")


def v2(**kw):
    """A v2 structured_output dict with every key present; kw overrides."""
    base = dict(label="steer", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None, parts=[])
    base.update(kw)
    return base


def v2_part(**kw):
    base = dict(label="steer", text="x", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None)
    base.update(kw)
    return base


def test_run_parses_structured_output(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": v2(label="queue", depends_on=["p1"])}))
    d = classify.run({"message": "x"}, claude=FAKE)
    assert d == classify.Decision("queue", 0.95, ["p1"])
    assert d.interrupt is False and d.parked is False and d.target is None and d.note is None and d.parts == []


def test_run_rejects_bad_label(monkeypatch):
    # "interrupt" is a flag in v2, no longer a label.
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": v2(label="interrupt", confidence=0.9, interrupt=True)}))
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
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": v2(label="queue", confidence=1.5)}))
    assert classify.run({"message": "x"}, claude=FAKE) is None


def test_run_rejects_non_string_depends_on(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": v2(label="queue", confidence=0.9, depends_on=[1])}))
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
    assert set(s["required"]) == {"label", "confidence", "interrupt", "parked", "depends_on", "target", "note", "parts"}
    assert s["properties"]["label"]["enum"] == ["steer", "queue", "amend", "split", "unclear"]
    assert s["properties"]["confidence"]["minimum"] == 0 and s["properties"]["confidence"]["maximum"] == 1
    assert s["properties"]["depends_on"]["items"]["type"] == "string"
    part = s["properties"]["parts"]["items"]
    assert part["type"] == "object" and part["additionalProperties"] is False
    assert set(part["required"]) == {"label", "text", "confidence", "interrupt", "parked", "depends_on", "target", "note"}
    assert part["properties"]["label"]["enum"] == ["steer", "queue", "amend", "split", "unclear"]


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


# M1 Task 2: decision contract v2 (plan tests).

def test_v2_validate_roundtrip():
    obj = {"label": "split", "confidence": 0.95, "interrupt": False, "parked": False, "depends_on": [], "target": None, "note": None,
           "parts": [{"label": "steer", "text": "use argon2 instead", "confidence": 0.95, "interrupt": False, "parked": False, "depends_on": [], "target": None, "note": None},
                     {"label": "queue", "text": "add password reset", "confidence": 0.9, "interrupt": False, "parked": False, "depends_on": ["p1"], "target": None, "note": None}]}
    d = classify._validate(obj)
    assert d.label == "split" and d.parts[1].depends_on == ["p1"] and d.parts[0].text == "use argon2 instead"


def test_v2_validate_rejects_old_interrupt_label():
    assert classify._validate({"label": "interrupt", "confidence": 1, "interrupt": True, "parked": False, "depends_on": [], "target": None, "note": None, "parts": []}) is None


def test_v2_validate_rejects_bad_note():
    base = {"label": "amend", "confidence": 0.9, "interrupt": False, "parked": False, "depends_on": [], "target": "p3", "note": "delete", "parts": []}
    assert classify._validate(base) is None


def test_payload_includes_candidates():
    p = classify.payload("running", None, [], [], "for the tests, include a rate-limit case", candidates=[{"id": "p3", "text": "Write tests for /login", "score": 0.71}])
    assert p["candidates"][0]["id"] == "p3"


def test_schema_matches_dataclass_fields():
    assert set(classify.SCHEMA["properties"]) == {f.name for f in dataclasses.fields(classify.Decision)}
    assert set(classify.SCHEMA["properties"]["parts"]["items"]["properties"]) == {f.name for f in dataclasses.fields(classify.Part)}


# Additional v2 tests beyond the plan.

def test_labels_v2():
    assert classify.LABELS == ("steer", "queue", "amend", "split", "unclear")


def test_v2_validate_accepts_every_note_value():
    for note in ("approve", "revise plan", "cancel", "pause", None):
        d = classify._validate(v2(label="amend", target="p2", note=note))
        assert d is not None and d.note == note and d.target == "p2"


def test_v2_validate_interrupt_flag_on_steer():
    d = classify._validate(v2(label="steer", interrupt=True))
    assert d.label == "steer" and d.interrupt is True and d.parked is False


def test_v2_validate_parked_flag_on_queue():
    d = classify._validate(v2(label="queue", parked=True, depends_on=["p1"]))
    assert d.parked is True and d.depends_on == ["p1"]


def test_v2_validate_rejects_missing_key():
    obj = v2(); del obj["parts"]
    assert classify._validate(obj) is None
    obj = v2(); del obj["interrupt"]
    assert classify._validate(obj) is None


def test_v2_validate_rejects_extra_key():
    assert classify._validate(v2(extra=1)) is None


def test_v2_validate_rejects_wrong_types():
    assert classify._validate(v2(interrupt="yes")) is None
    assert classify._validate(v2(parked=1)) is None
    assert classify._validate(v2(target=3)) is None
    assert classify._validate(v2(parts={})) is None
    assert classify._validate(v2(confidence=True)) is None
    assert classify._validate(v2(depends_on="p1")) is None


def test_v2_validate_rejects_bad_part():
    assert classify._validate(v2(label="split", parts=[v2_part(label="queue")])) is not None
    assert classify._validate(v2(label="split", parts=[v2_part(label="interrupt")])) is None
    bad = v2_part(); del bad["text"]
    assert classify._validate(v2(label="split", parts=[bad])) is None
    assert classify._validate(v2(label="split", parts=[v2_part(extra=1)])) is None
    assert classify._validate(v2(label="split", parts=[v2_part(note="delete")])) is None
    assert classify._validate(v2(label="split", parts=[v2_part(text=5)])) is None
    assert classify._validate(v2(label="split", parts=[v2_part(confidence=2)])) is None
    assert classify._validate(v2(label="split", parts=[v2_part(depends_on=[None])])) is None
    assert classify._validate(v2(label="split", parts=["not a part"])) is None


def test_v2_validate_part_types():
    d = classify._validate(v2(label="split", parts=[v2_part(label="queue", confidence=1, parked=True, depends_on=["p1"])]))
    p = d.parts[0]
    assert isinstance(p, classify.Part) and p.parked is True and p.confidence == 1.0 and isinstance(p.confidence, float)
    assert p.depends_on == ["p1"] and p.text == "x"


def test_decision_positional_construction_matches_m0():
    # tests/test_intake.py builds Decision(label, confidence, depends_on); the v2 fields default.
    d = classify.Decision("queue", 0.95, ["p1"])
    assert d.depends_on == ["p1"] and d.interrupt is False and d.parked is False
    assert d.target is None and d.note is None and d.parts == []


def test_decision_asdict_includes_parts():
    d = classify.Decision("split", 0.95, [], parts=[classify.Part("steer", "a", 0.95)])
    as_dict = dataclasses.asdict(d)
    assert as_dict["parts"][0]["text"] == "a" and as_dict["parts"][0]["depends_on"] == []
    assert json.dumps(as_dict)


def test_payload_without_candidates_has_no_key():
    p = classify.payload("running", None, [], [], "msg")
    assert "candidates" not in p
    p = classify.payload("running", None, [], [], "msg", candidates=[])
    assert p["candidates"] == []


def test_run_parses_v2_split(monkeypatch):
    out = v2(label="split", parts=[v2_part(label="steer", text="use argon2 instead"),
                                   v2_part(label="queue", text="add password reset", confidence=0.9, depends_on=["p1"])])
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": out}))
    d = classify.run({"message": "x"}, claude=FAKE)
    assert d.label == "split" and [p.label for p in d.parts] == ["steer", "queue"]
    assert d.parts[1].depends_on == ["p1"]


def test_schema_target_and_note_are_nullable():
    props = classify.SCHEMA["properties"]
    assert '"null"' in json.dumps(props["target"])
    assert '"null"' in json.dumps(props["note"])
    note_text = json.dumps(props["note"])
    for value in ("approve", "revise plan", "cancel", "pause"):
        assert f'"{value}"' in note_text
    assert '"delete"' not in note_text


def test_prompt_v2_content():
    text = classify.PROMPT_PATH.read_text()
    assert text.rstrip().endswith("Answer with JSON only.")
    for word in ("`amend`", "`split`", "`parked`", "`interrupt`", "`awaiting_approval`", "`approve`", "`revise plan`", "`cancel`", "`pause`", "`candidates`"):
        assert word in text, word
    assert '"label": "interrupt"' not in text and '| `interrupt` |' not in text  # a flag, no longer a label
    for case in ("g04", "g07", "g08", "g09", "g12", "g14", "g16", "g18", "g20", "g23", "g25", "g27", "g31", "g32",
                 "g33", "g34", "g36", "g37", "g41", "g42", "g43", "g45", "g46", "g47"):
        assert f"({case})" in text, case
