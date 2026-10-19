import json
from pathlib import Path
import subprocess
import sys

from qsc import classify, intake, paths, store

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "intake.py"


def ev(prompt, prompt_id="pid-1", sid="s1"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt_id": prompt_id, "prompt": prompt, "transcript_path": None}


def test_new_turn_registers_running_task(data_dir):
    assert intake.handle(ev("add a user model")) is None
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p1" and s.tasks[0].status == "running"


def test_mid_turn_marker_queues_and_blocks(data_dir):
    intake.handle(ev("add a user model"))
    out = intake.handle(ev("later: email verification"))
    assert out["decision"] == "block" and "p2" in out["reason"]
    s = store.load("s1"); assert s.tasks[1].status == "queued" and s.tasks[1].depends_on == ["p1"]
    intake.handle(ev("later: password reset"))
    assert store.load("s1").get("p3").depends_on == ["p2"]


def test_mid_turn_stop_sets_interrupt(data_dir):
    intake.handle(ev("add a user model"))
    out = intake.handle(ev("stop"))
    assert out["decision"] == "block" and store.load("s1").interrupt is True


def test_mid_turn_undecided_uses_classifier_threshold(data_dir):
    intake.handle(ev("add a user model"))
    low = lambda p: classify.Decision("queue", 0.6, ["p1"])
    assert intake.handle(ev("add password reset as well"), classifier=low) is None
    high = lambda p: classify.Decision("queue", 0.95, ["p1"])
    assert intake.handle(ev("add password reset as well"), classifier=high)["decision"] == "block"


def test_classifier_failure_passes_through(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("add password reset as well"), classifier=lambda p: None) is None


def test_shadow_logs_but_does_not_block(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("later: email verification"), shadow=True) is None
    s = store.load("s1")
    # Issue 2: shadow mode records the would-be task under a status ready() never picks.
    assert s.get("p2").status == "shadow_queued" and s.get("p2").depends_on == ["p1"] and s.ready() == []
    assert intake.handle(ev("later: password reset"), shadow=True) is None
    assert store.load("s1").get("p3").depends_on == ["p2"]
    assert '"label": "queue"' in paths.log_path("s1").read_text()


def test_shadow_interrupt_logs_only(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("stop"), shadow=True) is None
    assert store.load("s1").interrupt is False


def test_block_output_is_visible_to_the_user(data_dir):
    intake.handle(ev("add a user model"))
    for msg in ("later: email verification", "stop"):
        out = intake.handle(ev(msg))
        assert out["decision"] == "block"
        # Issue 1: reason is not displayed on 2.1.302; systemMessage is, and the
        # held text must not be echoed back as "Original prompt: ...".
        assert out["systemMessage"] == out["reason"]
        assert out["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}


def test_esc_detected_drops_queue_with_notice(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    assert "dropped queued tasks: p2" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.get("p1").status == "interrupted" and s.get("p2").status == "dropped" and s.running_task == "p3"


def test_pass_rule_never_touches_store(data_dir):
    assert intake.handle(ev("ok")) is None and not paths.store_path("s1").exists()


def test_script_swallows_bad_stdin():
    r = subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


# --- additional tests beyond the plan ---------------------------------------

def test_classifier_steer_label_passes_through(data_dir):
    intake.handle(ev("add a user model"))
    steer = lambda p: classify.Decision("steer", 0.99, [])
    assert intake.handle(ev("use bcrypt for the hashing"), classifier=steer) is None
    assert store.load("s1").tasks[-1].id == "p1"


def test_classifier_interrupt_sets_flag(data_dir):
    intake.handle(ev("add a user model"))
    stop = lambda p: classify.Decision("interrupt", 0.95, [])
    out = intake.handle(ev("please halt everything you are doing"), classifier=stop)
    assert out["decision"] == "block" and store.load("s1").interrupt is True


def test_classifier_sees_state_queue_and_message(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    seen = {}

    def spy(p):
        seen.update(p)
        return None

    intake.handle(ev("add password reset as well"), classifier=spy)
    assert seen["state"] == "running" and seen["running_task"]["id"] == "p1"
    assert [q["id"] for q in seen["queue"]] == ["p2"] and seen["message"] == "add password reset as well"


def test_classifier_depends_on_overrides_chain(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    direct = lambda p: classify.Decision("queue", 0.95, ["p1"])
    out = intake.handle(ev("add password reset as well"), classifier=direct)
    assert out["decision"] == "block" and store.load("s1").get("p3").depends_on == ["p1"]


def test_classifier_empty_depends_on_uses_chain(data_dir):
    intake.handle(ev("add a user model"))
    bare = lambda p: classify.Decision("queue", 0.95, [])
    intake.handle(ev("add password reset as well"), classifier=bare)
    assert store.load("s1").get("p2").depends_on == ["p1"]


def test_esc_with_empty_queue_has_no_notice(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("something else", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.get("p1").status == "interrupted" and s.running_task == "p2"


def test_log_records_classifier_null_on_failure(data_dir):
    intake.handle(ev("add a user model"))
    intake.handle(ev("add password reset as well"), classifier=lambda p: None)
    rows = [json.loads(line) for line in paths.log_path("s1").read_text().splitlines()]
    last = rows[-1]
    assert last["event"] == "intake" and last["classifier"] is None and last["label"] == "steer"


def test_script_end_to_end_queues_via_stdin(data_dir):
    def run(prompt, pid="pid-1"):
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(ev(prompt, pid)),
                              capture_output=True, text=True)

    r1 = run("add a user model")
    assert r1.returncode == 0 and r1.stdout == ""
    r2 = run("later: email verification")
    assert r2.returncode == 0 and json.loads(r2.stdout)["decision"] == "block"
    assert store.load("s1").get("p2").status == "queued"


def test_script_shadow_env_does_not_block(data_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW", "true")
    run = lambda p: subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(ev(p)), capture_output=True, text=True)
    run("add a user model")
    r = run("later: email verification")
    assert r.returncode == 0 and r.stdout == ""
    t = store.load("s1").tasks[-1]
    assert t.id == "p2" and t.status == "shadow_queued"


def test_interrupt_reason_names_running_task(data_dir):
    intake.handle(ev("add a user model"))
    assert "p1" in intake.handle(ev("stop"))["reason"]
