import json
import time
from pathlib import Path

from qsc import done, model, store

ROOT = Path(__file__).resolve().parent.parent
FAKE = str(ROOT / "tests" / "fakes" / "claude")


# M2 Task 4 plan tests.

def test_run_verify_ok_and_failed(tmp_path):
    assert done.run_verify("true", str(tmp_path)).verdict == "verify_ok"
    v = done.run_verify("echo boom; exit 3", str(tmp_path))
    assert v.verdict == "verify_failed" and v.reason == "exit 3" and "boom" in v.output_tail


def test_run_verify_timeout(tmp_path):
    t0 = time.time(); v = done.run_verify("sleep 5", str(tmp_path), timeout=0.5)
    assert v.verdict == "verify_failed" and "timeout" in v.reason and time.time() - t0 < 3     # review focus 3


def test_judge_maps_and_fails_open(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"verdict": "asked_question", "confidence": 0.9, "reason": "asks which DB"}}))
    assert done.judge("add login", [], "", "Which database should I use?", claude=FAKE).verdict == "asked_question"
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", "not json")
    assert done.judge("add login", [], "", "x", claude=FAKE).verdict == "done"


def test_judge_low_confidence_not_done_is_done(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"verdict": "not_done", "confidence": 0.4, "reason": "meh"}}))
    assert done.judge("a", [], "", "did some of it", claude=FAKE).verdict == "done"


def test_decide_prefers_verify(tmp_path):
    t = store.Task(id="p1", text="x", verify="true")
    assert done.decide(t, {"cwd": str(tmp_path), "last_assistant_message": "anything"}, "").verdict == "verify_ok"


def test_decide_empty_closing_is_stopped():
    t = store.Task(id="p1", text="x")
    assert done.decide(t, {"last_assistant_message": ""}, "").verdict == "stopped"


# Additional tests beyond the plan.

def test_verdicts_constant():
    assert done.VERDICTS == ("verify_ok", "verify_failed", "done", "not_done", "asked_question", "gave_up", "stopped", "confirmed")


def test_verdict_dataclass_defaults():
    v = done.Verdict("done", "r")
    assert (v.verdict, v.reason, v.output_tail) == ("done", "r", "")


def test_judge_schema_shape():
    s = done.JUDGE_SCHEMA
    assert s["type"] == "object" and s["additionalProperties"] is False
    assert set(s["required"]) == {"verdict", "confidence", "reason"} == set(s["properties"])
    assert s["properties"]["verdict"]["enum"] == ["done", "not_done", "asked_question"]
    assert s["properties"]["confidence"]["type"] == "number" and s["properties"]["reason"]["type"] == "string"


def test_judge_prompt_file_exists():
    assert done.JUDGE_PROMPT == ROOT / "prompts" / "judge_system.md" and done.JUDGE_PROMPT.is_file()
    text = done.JUDGE_PROMPT.read_text()
    for word in ("done", "not_done", "asked_question", "JSON"):
        assert word in text


def test_run_verify_tail_is_capped_and_combined(tmp_path):
    cmd = "python3 -c \"import sys; sys.stdout.write('o'*2000); sys.stdout.flush(); sys.stderr.write('E'*100); sys.exit(1)\""
    v = done.run_verify(cmd, str(tmp_path))
    assert v.verdict == "verify_failed" and v.reason == "exit 1"
    assert len(v.output_tail) == 1500 and v.output_tail.endswith("E" * 100)


def test_run_verify_runs_in_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    assert done.run_verify("test -f marker.txt", str(tmp_path)).verdict == "verify_ok"


def test_run_verify_timeout_reason_names_the_timeout(tmp_path):
    v = done.run_verify("sleep 5", str(tmp_path), timeout=0.2)
    assert v.reason == "timeout after 0.2 s"


def test_run_verify_bad_cwd_is_verify_failed(tmp_path):
    v = done.run_verify("true", str(tmp_path / "missing"))
    assert v.verdict == "verify_failed" and v.reason


def test_judge_payload_and_call(monkeypatch):
    seen = {}
    def fake_call(prompt_path, schema, payload, timeout=15.0, claude="claude", model="haiku"):
        seen.update(prompt_path=prompt_path, schema=schema, payload=payload, timeout=timeout, claude=claude, model=model)
        return {"verdict": "done", "confidence": 0.95, "reason": "complete"}
    monkeypatch.setattr(model, "call", fake_call)
    v = done.judge("add login", ["use jwt"], "# plan", "Done: /login added.", claude="/bin/c")
    assert v == done.Verdict("done", "complete")
    assert seen["prompt_path"] == done.JUDGE_PROMPT and seen["schema"] is done.JUDGE_SCHEMA
    assert seen["payload"] == {"task": "add login", "notes": ["use jwt"], "plan": "# plan", "closing": "Done: /login added."}
    assert seen["claude"] == "/bin/c" and seen["model"] == "haiku" and seen["timeout"] == 15.0


def test_judge_fail_open_reason(monkeypatch):
    monkeypatch.setattr(model, "call", lambda *a, **k: None)
    assert done.judge("a", [], "", "x") == done.Verdict("done", "judge unavailable; fail open")


def test_judge_invalid_answer_fails_open(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"verdict": "maybe", "confidence": 0.9, "reason": "r"}}))
    assert done.judge("a", [], "", "x", claude=FAKE).verdict == "done"
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"verdict": "not_done", "confidence": "high", "reason": "r"}}))
    assert done.judge("a", [], "", "x", claude=FAKE).verdict == "done"


def test_judge_confident_not_done_stays_not_done(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"verdict": "not_done", "confidence": 0.7, "reason": "half"}}))
    v = done.judge("a", [], "", "did half", claude=FAKE)
    assert v.verdict == "not_done" and v.reason == "half"


def test_judge_low_confidence_asked_question_is_kept(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"structured_output": {"verdict": "asked_question", "confidence": 0.3, "reason": "q"}}))
    assert done.judge("a", [], "", "which?", claude=FAKE).verdict == "asked_question"


def test_decide_calls_judge_through_module_attribute(monkeypatch):
    seen = {}
    def fake_judge(task_text, notes, plan_excerpt, closing, claude="claude"):
        seen.update(task_text=task_text, notes=notes, plan_excerpt=plan_excerpt, closing=closing, claude=claude)
        return done.Verdict("not_done", "r")
    monkeypatch.setattr(done, "judge", fake_judge)
    t = store.Task(id="p1", text="x", notes=["n1"])
    v = done.decide(t, {"last_assistant_message": "closing words"}, "# plan", claude="/bin/c")
    assert v.verdict == "not_done"
    assert seen == {"task_text": "x", "notes": ["n1"], "plan_excerpt": "# plan", "closing": "closing words", "claude": "/bin/c"}


def _no_judge(*a, **k):
    raise AssertionError("judge must not be called")


def test_decide_verify_skips_judge(monkeypatch, tmp_path):
    monkeypatch.setattr(done, "judge", _no_judge)
    t = store.Task(id="p1", text="x", verify="exit 4")
    v = done.decide(t, {"cwd": str(tmp_path), "last_assistant_message": ""}, "")
    assert v.verdict == "verify_failed" and v.reason == "exit 4"


def test_decide_missing_or_blank_closing_is_stopped(monkeypatch):
    monkeypatch.setattr(done, "judge", _no_judge)
    t = store.Task(id="p1", text="x")
    assert done.decide(t, {}, "") == done.Verdict("stopped", "no closing message")
    assert done.decide(t, {"last_assistant_message": "   \n"}, "").verdict == "stopped"
    assert done.decide(t, {"last_assistant_message": None}, "").verdict == "stopped"


# --- M3 Task 3: verify runs in its own process group; `confirmed` is a verdict

def test_run_verify_kills_process_group(tmp_path):
    marker = tmp_path / "alive"
    cmd = f"(sleep 30; touch {marker}) & sleep 30"
    v = done.run_verify(cmd, str(tmp_path), timeout=0.5)
    time.sleep(1.0)
    assert v.verdict == "verify_failed" and not marker.exists()
    assert "confirmed" in done.VERDICTS


# Additional tests beyond the plan's one.

def test_run_verify_starts_a_new_session(tmp_path):
    import os
    cmd = f"python3 -c \"import os, sys; sys.exit(0 if os.getpgid(0) != {os.getpgid(0)} else 1)\""
    assert done.run_verify(cmd, str(tmp_path)).verdict == "verify_ok"


def test_run_verify_background_child_dies_with_the_group(tmp_path):
    import os
    pidfile = tmp_path / "pid"
    # `$$` inside `sh -c` is that inner shell's pid, which `exec` hands to sleep.
    cmd = f"sh -c 'echo $$ > {pidfile}; exec sleep 30' & sleep 30"
    t0 = time.time(); v = done.run_verify(cmd, str(tmp_path), timeout=0.5)
    assert v.verdict == "verify_failed" and time.time() - t0 < 3
    pid = int(pidfile.read_text().strip())
    deadline = time.time() + 2
    alive = True
    while alive and time.time() < deadline:
        try:
            os.kill(pid, 0); time.sleep(0.05)
        except ProcessLookupError:
            alive = False
    assert not alive


def test_run_verify_timeout_keeps_partial_output(tmp_path):
    v = done.run_verify("echo partial; sleep 5", str(tmp_path), timeout=0.5)
    assert v.verdict == "verify_failed" and "partial" in v.output_tail
