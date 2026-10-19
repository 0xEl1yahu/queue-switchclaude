"""Task 8: the golden-set eval runner (`eval/run_golden.py`)."""
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import run_golden  # noqa: E402
from qsc import classify  # noqa: E402

G = {json.loads(l)["id"]: json.loads(l) for l in open(ROOT / "eval" / "golden_queue_intake.jsonl")}

FAKE = str(ROOT / "tests" / "fakes" / "claude")


def dec(**kw):
    """A classify.Decision for whichever contract (M0 v1 or M1 v2) is installed."""
    defaults = dict(label="steer", confidence=0.95, depends_on=[], interrupt=False, parked=False, target=None, note=None, parts=[])
    defaults.update(kw)
    names = {f.name for f in dataclasses.fields(classify.Decision)}
    return classify.Decision(**{k: v for k, v in defaults.items() if k in names})


def test_decide_rules_only_matches_known_cases():
    assert run_golden.decide(G["g03"], classifier=None)["interrupt"] is True
    assert run_golden.decide(G["g06"], classifier=None) == {"label": "queue", "id": "p2", "depends_on": ["p1"], "parked": False, "interrupt": False}
    assert run_golden.decide(G["g01"], classifier=None)["label"] == "steer"      # undecided + no classifier = steer


def test_cli_rules_only_runs_and_gates(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "run_golden.py"), "--rules-only", "--report", str(tmp_path / "r.md")], capture_output=True, text=True, cwd=ROOT)
    assert "g47" in r.stdout and (tmp_path / "r.md").exists() and r.returncode in (0, 1)


def test_cli_missing_claude_no_traceback(tmp_path, monkeypatch):
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "run_golden.py"), "--cases", "g01", "--no-cache"], capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PATH": "/nonexistent"})
    assert "Traceback" not in r.stderr and r.returncode == 1                       # review focus 5


# --- additional tests beyond the plan ---------------------------------------

def test_decide_ids_continue_from_the_known_tasks():
    # Context B knows p1 (done), p2 (running) and p3 (queued): the next id is p4, after the last queued task.
    got = run_golden.decide({**G["g13"], "message": "later: set up CI to run the tests"}, classifier=None)
    assert got == {"label": "queue", "id": "p4", "depends_on": ["p3"], "parked": False, "interrupt": False}
    # Context C is idle with nothing queued: no chain dependency.
    got = run_golden.decide({**G["g17"], "message": "later: add the login endpoint"}, classifier=None)
    assert got == {"label": "queue", "id": "p2", "depends_on": [], "parked": False, "interrupt": False}


def test_decide_rules_never_call_the_classifier():
    calls = []
    run_golden.decide(G["g06"], classifier=lambda p: calls.append(p))
    run_golden.decide(G["g03"], classifier=lambda p: calls.append(p))
    assert calls == []


def test_decide_model_queue_dependencies_override_chain():
    seen = {}

    def clf(p):
        seen.update(p)
        return dec(label="queue", depends_on=["p2"])

    got = run_golden.decide(G["g12"], classifier=clf)
    assert got == {"label": "queue", "id": "p4", "depends_on": ["p2"], "parked": False, "interrupt": False}
    assert seen["state"] == "running" and seen["running_task"] == {"id": "p2", "text": "Add a /login endpoint that returns a JWT"}
    assert [q["id"] for q in seen["queue"]] == ["p1", "p3"] and seen["message"] == G["g12"]["message"]


def test_decide_model_empty_depends_on_falls_back_to_chain_like_intake():
    got = run_golden.decide(G["g08"], classifier=lambda p: dec(label="queue", depends_on=[]))
    assert got["label"] == "queue" and got["depends_on"] == ["p1"]


def test_decide_low_confidence_and_failure_are_steer():
    assert run_golden.decide(G["g07"], classifier=lambda p: dec(label="queue", confidence=0.6, depends_on=["p1"])) == {"label": "steer", "interrupt": False}
    assert run_golden.decide(G["g07"], classifier=lambda p: None) == {"label": "steer", "interrupt": False}


def test_decide_payload_carries_recent_turns():
    seen = {}

    def clf(p):
        seen.update(p)
        return None

    run_golden.decide(G["g31"], classifier=clf)
    assert seen["recent"] == G["g31"]["recent"]


def test_cache_path_is_keyed_by_case_and_hash(tmp_path):
    a = run_golden.cache_path("g01", "prompt", {"message": "x"}, cache_dir=tmp_path)
    b = run_golden.cache_path("g01", "prompt", {"message": "y"}, cache_dir=tmp_path)
    assert a.parent == tmp_path and a.name.startswith("g01-") and a.suffix == ".json" and a != b
    assert a == run_golden.cache_path("g01", "prompt", {"message": "x"}, cache_dir=tmp_path)


def test_classify_cached_reuses_the_answer(tmp_path, monkeypatch):
    calls = []

    def fake_run(payload, **kw):
        calls.append(payload)
        return dec(label="queue", depends_on=["p1"])

    monkeypatch.setattr(run_golden.classify, "run", fake_run)
    monkeypatch.setattr(run_golden.classify, "_system_prompt", lambda: "prompt")
    stats = {}
    first = run_golden.classify_cached("g07", {"message": "x"}, cache_dir=tmp_path, stats=stats)
    assert first.label == "queue" and stats["cached"] is False and len(calls) == 1
    second = run_golden.classify_cached("g07", {"message": "x"}, cache_dir=tmp_path, stats=stats)
    assert second == first and stats["cached"] is True and len(calls) == 1
    run_golden.classify_cached("g07", {"message": "x"}, cache_dir=tmp_path, use_cache=False, stats=stats)
    assert len(calls) == 2


def test_classify_cached_does_not_cache_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(run_golden.classify, "run", lambda payload, **kw: None)
    monkeypatch.setattr(run_golden.classify, "_system_prompt", lambda: "prompt")
    stats = {}
    assert run_golden.classify_cached("g07", {"message": "x"}, cache_dir=tmp_path, stats=stats) is None
    assert stats["error"] is True and list(tmp_path.iterdir()) == []


def test_cli_with_fake_claude_passes_g01(tmp_path):
    out = {k: v for k, v in dict(label="steer", confidence=0.95, depends_on=[], interrupt=False, parked=False, target=None, note=None, parts=[]).items()
           if k in classify.SCHEMA["properties"]}
    env = {**os.environ, "FAKE_CLAUDE_OUTPUT": json.dumps({"structured_output": out}), "QSC_EVAL_CACHE": str(tmp_path / "cache")}
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "run_golden.py"), "--claude", FAKE, "--cases", "g01,g03", "--report", str(tmp_path / "r.md")],
                       capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "gate: PASS" in r.stdout and "model" in r.stdout and (tmp_path / "r.md").read_text().count("| g0") == 2
    assert any(p.name.startswith("g01-") for p in (tmp_path / "cache").iterdir())


def test_cli_unknown_case_is_an_error_not_a_traceback():
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "run_golden.py"), "--rules-only", "--cases", "g99"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2 and "Traceback" not in r.stderr and "g99" in r.stderr


def test_split_parts_are_confidence_gated_like_intake():
    row = G["g09"]
    d = classify.Decision(label="split", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None,
                          parts=[classify.Part(label="steer", text="use argon2 instead", confidence=0.95),
                                 classify.Part(label="queue", text="add password reset", confidence=0.5, depends_on=["p1"])])
    got = run_golden.decide(row, classifier=lambda p: d)
    assert got["label"] in ("steer", "split")
    if got["label"] == "split":
        assert [p["label"] for p in got["parts"]] == ["steer", "steer"]


def test_amend_rewire_only_when_model_named_target():
    row = G["g23"]   # queue holds p2 "Add a Stripe webhook handler"; message is about it
    d = classify.Decision(label="amend", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None, parts=[])
    got = run_golden.decide(row, classifier=lambda p: d)
    assert got["label"] == "amend" and got["target"] == "p2" and "depends_on" not in got
