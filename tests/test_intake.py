import json
from pathlib import Path
import subprocess
import sys

from qsc import classify, hookio, intake, paths, store

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "intake.py"


def ev(prompt, prompt_id="pid-1", sid="s1"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt_id": prompt_id, "prompt": prompt, "transcript_path": None}


def dec(**kw):
    base = dict(label="steer", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None, parts=[])
    base.update(kw); return classify.Decision(**base)


def part(**kw):
    base = dict(label="steer", text="", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None)
    base.update(kw); return classify.Part(**base)


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
    low = lambda p: dec(label="queue", confidence=0.6, depends_on=["p1"])
    assert intake.handle(ev("add password reset as well"), classifier=low) is None
    high = lambda p: dec(label="queue", depends_on=["p1"])
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
    steer = lambda p: dec(label="steer", confidence=0.99)
    assert intake.handle(ev("use bcrypt for the hashing"), classifier=steer) is None
    assert store.load("s1").tasks[-1].id == "p1"


def test_classifier_interrupt_sets_flag(data_dir):
    intake.handle(ev("add a user model"))
    stop = lambda p: dec(label="steer", interrupt=True)
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
    direct = lambda p: dec(label="queue", depends_on=["p1"])
    out = intake.handle(ev("add password reset as well"), classifier=direct)
    assert out["decision"] == "block" and store.load("s1").get("p3").depends_on == ["p1"]


def test_classifier_empty_depends_on_uses_chain(data_dir):
    intake.handle(ev("add a user model"))
    bare = lambda p: dec(label="queue")
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


# --- M1 Task 6: control, dedupe, parked, amend, split ------------------------

def test_control_list_blocks_with_queue(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    out = intake.handle(ev("queue: list"))
    assert out["decision"] == "block" and "p2" in out["systemMessage"]


def test_dedupe_exact_resubmission(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    out = intake.handle(ev("later: email verification", prompt_id="pid-2"))          # new turn after the block put it back
    assert "already p2" in out["systemMessage"] and store.load("s1").next_id == 3


def test_dedupe_is_exact_not_fuzzy(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: add goodbye.py"))
    assert intake.handle(ev("later: add a goodbye.py"))["systemMessage"].startswith("queue-switchclaude: queued p3")   # review focus 1


def test_parked_marker_parks(data_dir):
    intake.handle(ev("add a user model"))
    out = intake.handle(ev("maybe later we can add social login"))
    assert "parked p2" in out["systemMessage"] and store.load("s1").get("p2").status == "parked" and store.load("s1").ready() == []


def test_amend_cancel_by_model_target(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests for login"))
    out = intake.handle(ev("skip the tests for now, I'll write them myself"), classifier=lambda p: dec(label="amend", target="p2", note="cancel"))
    assert "cancelled p2" in out["systemMessage"] and store.load("s1").get("p2").status == "cancelled"


def test_amend_target_by_match_when_model_gives_none(data_dir):
    intake.handle(ev("refactor the billing service")); intake.handle(ev("later: add a Stripe webhook handler"))
    out = intake.handle(ev("the webhook handler should verify Stripe signatures"), classifier=lambda p: dec(label="amend"))
    assert "noted on p2" in out["systemMessage"] and store.load("s1").get("p2").notes == ["the webhook handler should verify Stripe signatures"]


def test_amend_ambiguous_target_falls_to_steer(data_dir):
    intake.handle(ev("x")); intake.handle(ev("later: write unit tests for billing")); intake.handle(ev("later: write unit tests for login"))
    assert intake.handle(ev("for the tests, add a rate-limit case"), classifier=lambda p: dec(label="amend")) is None   # review focus 2


def test_amend_rewires_dependencies(data_dir):
    intake.handle(ev("refactor billing")); intake.handle(ev("later: add webhook"))
    out = intake.handle(ev("actually do the webhook first, refactor can wait"), classifier=lambda p: dec(label="amend", target="p2", depends_on=[]))
    assert store.load("s1").get("p2").depends_on == [] and "p2 now depends on nothing" in out["systemMessage"]


def test_split_steer_plus_queue_passes_with_context(data_dir):
    intake.handle(ev("add a user model"))
    d = dec(label="split", parts=[part(label="steer", text="use argon2 instead"), part(label="queue", text="add password reset", depends_on=["p1"])])
    out = intake.handle(ev("use argon2 instead, and after that add password reset"), classifier=lambda p: d)
    assert "decision" not in out and "queued p2" in out["hookSpecificOutput"]["additionalContext"] and "use argon2 instead" in out["hookSpecificOutput"]["additionalContext"]
    assert store.load("s1").get("p2").depends_on == ["p1"]


def test_split_all_steer_is_plain_steer(data_dir):
    intake.handle(ev("add a user model"))
    d = dec(label="split", parts=[part(label="steer", text="a"), part(label="steer", text="b")])
    assert intake.handle(ev("do a and b"), classifier=lambda p: d) is None and store.load("s1").next_id == 2      # review focus 3


def test_split_all_queue_blocks_with_both(data_dir):
    intake.handle(ev("x"))
    d = dec(label="split", parts=[part(label="queue", text="login endpoint", depends_on=["p1"]), part(label="queue", text="logout endpoint", depends_on=["p2"])])
    out = intake.handle(ev("1) login endpoint 2) logout endpoint"), classifier=lambda p: d)
    assert out["decision"] == "block" and store.load("s1").get("p3").depends_on == ["p2"]


def test_split_remaps_predicted_part_ids_after_a_cancel(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification")); intake.handle(ev("queue: cancel p2"))
    # The model sees p1 running and an empty queue (p2 is cancelled, so hidden), numbers the
    # parts p2 and p3, and chains the second to "p2". The store hands out p3 and p4.
    d = dec(label="split", parts=[part(label="queue", text="login endpoint", depends_on=["p1"]),
                                  part(label="queue", text="logout endpoint", depends_on=["p2"])])
    out = intake.handle(ev("1) login endpoint 2) logout endpoint"), classifier=lambda p: d)
    s = store.load("s1")
    assert s.get("p3").depends_on == ["p1"] and s.get("p4").depends_on == ["p3"]
    assert "queued p4 after p3" in out["systemMessage"] and "after p2" not in out["systemMessage"]
    # A dependency on a task the model could see is left alone.
    assert s.get("p3").depends_on == ["p1"]


def test_low_confidence_amend_is_steer(data_dir):
    intake.handle(ev("x")); intake.handle(ev("later: y"))
    assert intake.handle(ev("tweak y"), classifier=lambda p: dec(label="amend", target="p2", confidence=0.5)) is None


# --- additional Task 6 tests beyond the plan ---------------------------------

def test_control_works_before_any_turn_and_is_not_a_turn(data_dir):
    out = intake.handle(ev("queue: list"))
    assert out["decision"] == "block" and out["systemMessage"] == "queue-switchclaude: queue is empty"
    s = store.load("s1"); assert s.state == "idle" and s.tasks == [] and s.running_task is None


def test_control_cancel_mid_turn_changes_store(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    out = intake.handle(ev("/queue cancel p2"))
    assert out["systemMessage"].startswith("queue-switchclaude: cancelled p2") and store.load("s1").get("p2").status == "cancelled"
    assert store.load("s1").state == "running" and store.load("s1").running_task == "p1"


def test_control_still_acts_in_shadow_mode(data_dir):
    intake.handle(ev("add a user model"))
    out = intake.handle(ev("queue: pause"), shadow=True)
    assert out["decision"] == "block" and store.load("s1").paused is True


def test_control_never_calls_classifier(data_dir):
    intake.handle(ev("add a user model"))
    called = []
    intake.handle(ev("queue: help"), classifier=lambda p: called.append(p))
    assert called == []
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert rows[-1]["rule"] == "control" and rows[-1]["label"] == "control"


def test_dedupe_logs_and_mid_turn(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    out = intake.handle(ev("later: email verification"))                                # same turn, same text
    assert "already p2 (queued)" in out["systemMessage"] and store.load("s1").next_id == 3
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert rows[-1]["decision"] == "dedupe" and rows[-1]["task"] == "p2"


def test_dedupe_skips_cancelled_tasks(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    intake.handle(ev("queue: cancel p2"))
    out = intake.handle(ev("later: email verification"))
    assert "queued p3" in out["systemMessage"]


def test_dedupe_never_swallows_an_interrupt(data_dir):
    # M3 Task 1 made "stop" at idle a no-op, so the task whose text is "stop"
    # is queued mid-turn instead of started as the turn itself.
    intake.handle(ev("add a user model")); intake.handle(ev("later: stop"))   # p2: a queued task whose text is "stop"
    out = intake.handle(ev("stop"))                 # mid-turn: this is an interrupt, not a duplicate of p2
    assert "stopping p1" in out["systemMessage"] and store.load("s1").interrupt is True


def test_dedupe_new_turn_retry_after_esc_is_not_a_duplicate(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("add a user model", prompt_id="pid-2")) is None       # Esc, then resend: a retry
    s = store.load("s1"); assert s.get("p1").status == "interrupted" and s.running_task == "p2"


def test_dedupe_in_shadow_logs_but_does_not_block(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"), shadow=True)
    assert intake.handle(ev("later: email verification"), shadow=True) is None
    assert store.load("s1").next_id == 3
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert rows[-1]["decision"] == "dedupe" and rows[-1]["shadow"] is True


def test_parked_marker_in_shadow_records_shadow_queued(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("someday: dark mode"), shadow=True) is None
    assert store.load("s1").get("p2").status == "shadow_queued"
    rows = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()]
    assert rows[-1]["parked"] is True


def test_parked_by_model_flag(data_dir):
    intake.handle(ev("add a user model"))
    out = intake.handle(ev("we could add social login eventually"), classifier=lambda p: dec(label="queue", parked=True))
    assert "parked p2" in out["systemMessage"] and store.load("s1").get("p2").status == "parked"


def test_parked_task_is_not_a_chain_dependency(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("maybe later we can add social login"))
    intake.handle(ev("later: email verification"))
    assert store.load("s1").get("p3").depends_on == ["p1"]


def test_amend_pause_marks_target_paused(data_dir):
    intake.handle(ev("add a user model"))
    out = intake.handle(ev("pause that for now"), classifier=lambda p: dec(label="amend", target="p1", note="pause"))
    assert out["systemMessage"] == "queue-switchclaude: paused p1" and store.load("s1").get("p1").status == "paused"


def test_amend_cancel_on_running_task_falls_to_steer(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("skip the user model"), classifier=lambda p: dec(label="amend", target="p1", note="cancel")) is None
    assert store.load("s1").get("p1").status == "running"


def test_amend_unknown_model_target_falls_back_to_match(data_dir):
    intake.handle(ev("refactor the billing service")); intake.handle(ev("later: add a Stripe webhook handler"))
    out = intake.handle(ev("the webhook handler should verify Stripe signatures"), classifier=lambda p: dec(label="amend", target="p9"))
    assert "noted on p2" in out["systemMessage"]


def test_amend_with_no_target_anywhere_is_steer(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("make it faster"), classifier=lambda p: dec(label="amend")) is None


def test_amend_payload_carries_candidates(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests for login"))
    seen = {}
    intake.handle(ev("for the tests, include a rate-limit case"), classifier=lambda p: seen.update(p))
    assert [c["id"] for c in seen["candidates"]][0] == "p2" and set(seen["candidates"][0]) == {"id", "text", "score"}


def test_amend_rewire_keeps_notes_when_deps_unchanged(data_dir):
    intake.handle(ev("refactor billing")); intake.handle(ev("later: add webhook"))
    out = intake.handle(ev("the webhook should retry"), classifier=lambda p: dec(label="amend", target="p2", depends_on=["p1"]))
    assert "noted on p2" in out["systemMessage"] and store.load("s1").get("p2").notes == ["the webhook should retry"]


def test_split_fake_parts_collapse_to_single_queue(data_dir):
    intake.handle(ev("x"))
    d = dec(label="split", parts=[part(label="queue", text="Add login"), part(label="queue", text="add login."), part(label="steer", text="   ")])
    out = intake.handle(ev("add login, add login"), classifier=lambda p: d)
    assert out["decision"] == "block" and "queued p2" in out["systemMessage"] and store.load("s1").next_id == 3


def test_split_with_amend_part_and_steer_part(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests for login"))
    d = dec(label="split", parts=[part(label="steer", text="use argon2"), part(label="amend", text="skip the tests", target="p2", note="cancel")])
    out = intake.handle(ev("use argon2, and skip the tests"), classifier=lambda p: d)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "cancelled p2" in ctx and ctx.endswith("act only on: use argon2") and store.load("s1").get("p2").status == "cancelled"


def test_split_low_confidence_part_is_steer(data_dir):
    intake.handle(ev("x"))
    d = dec(label="split", parts=[part(label="steer", text="a"), part(label="queue", text="b", confidence=0.4)])
    assert intake.handle(ev("a and b"), classifier=lambda p: d) is None and store.load("s1").next_id == 2


def test_split_in_shadow_records_shadow_queued_and_passes(data_dir):
    intake.handle(ev("x"))
    d = dec(label="split", parts=[part(label="steer", text="a"), part(label="queue", text="b")])
    assert intake.handle(ev("a and then b"), classifier=lambda p: d, shadow=True) is None
    assert store.load("s1").get("p2").status == "shadow_queued"


def test_unclear_label_is_steer(data_dir):
    intake.handle(ev("x"))
    assert intake.handle(ev("maybe later we can add this"), classifier=lambda p: dec(label="unclear")) is None
    assert store.load("s1").next_id == 2


def test_log_has_v2_fields(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests for login"))
    intake.handle(ev("skip the tests"), classifier=lambda p: dec(label="amend", target="p2", note="cancel"))
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["label"] == "amend" and row["target"] == "p2" and row["classifier"]["note"] == "cancel" and row["parts"] == [] and row["rule"] is None


# --- Task 10: shadow comparison ---------------------------------------------

def test_shadow_compare_logs_both(data_dir, monkeypatch):
    intake.handle(ev("add a user model"))
    calls = []
    def clf(p): calls.append(p); return dec(label="steer")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", "true")
    intake.handle(ev("later: email verification"), classifier=clf, shadow=True)
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert calls and row["rule_label"] == "queue" and row["classifier"]["label"] == "steer" and row["label"] == "queue"


def test_shadow_compare_still_records_shadow_queue(data_dir, monkeypatch):
    intake.handle(ev("add a user model"))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", "1")
    # The model says queue after p9; the acted decision stays the rule's (chain deps, shadow_queued).
    out = intake.handle(ev("later: email verification"), classifier=lambda p: dec(label="queue", depends_on=["p9"]), shadow=True)
    assert out is None
    t = store.load("s1").get("p2")
    assert t.status == "shadow_queued" and t.depends_on == ["p1"]


def test_shadow_compare_off_never_calls_classifier_for_rule_decisions(data_dir, monkeypatch):
    intake.handle(ev("add a user model"))
    calls = []
    def clf(p): calls.append(p); return dec(label="steer")
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", raising=False)
    intake.handle(ev("later: email verification"), classifier=clf, shadow=True)
    assert calls == []
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["rule_label"] == "queue" and row["classifier"] is None and row["label"] == "queue"


def test_shadow_compare_requires_shadow_mode(data_dir, monkeypatch):
    intake.handle(ev("add a user model"))
    calls = []
    def clf(p): calls.append(p); return dec(label="steer")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", "true")
    out = intake.handle(ev("later: email verification"), classifier=clf, shadow=False)
    assert calls == [] and out["decision"] == "block"


def test_shadow_compare_classifier_error_is_swallowed(data_dir, monkeypatch):
    intake.handle(ev("add a user model"))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", "true")
    def boom(p): raise RuntimeError("no model")
    assert intake.handle(ev("later: email verification"), classifier=boom, shadow=True) is None
    assert store.load("s1").get("p2").status == "shadow_queued"
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["classifier"] is None and row["label"] == "queue"


def test_intake_logs_classifier_latency_ms(data_dir):
    intake.handle(ev("add a user model"))
    intake.handle(ev("add password reset as well"), classifier=lambda p: dec(label="steer"))
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0
    intake.handle(ev("later: email verification"))
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["latency_ms"] is None and row["rule_label"] == "queue"


def test_hookio_shadow_compare_flag(monkeypatch):
    for v in ("1", "true", "YES"):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", v); assert hookio.shadow_compare()
    for v in ("0", "false", ""):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", v); assert not hookio.shadow_compare()
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE"); assert not hookio.shadow_compare()


def test_plugin_manifest_declares_shadow_compare():
    m = json.load(open(Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"))
    opt = m["userConfig"]["shadow_compare"]
    assert opt["type"] == "boolean" and opt["default"] is False and opt["title"] == "Shadow: also ask the model"


def test_steer_pause_note_pauses_running_task_and_passes_through(data_dir):
    intake.handle(ev("refactor billing"))
    out = intake.handle(ev("hold the refactor, something came up"), classifier=lambda p: dec(label="steer", note="pause", target="p1"))
    assert "decision" not in out and "paused p1" in out["hookSpecificOutput"]["additionalContext"]
    assert store.load("s1").get("p1").status == "paused"


def test_split_g27_pauses_running_and_rewires_queued(data_dir):
    intake.handle(ev("refactor the billing service")); intake.handle(ev("later: add a Stripe webhook handler"))
    d = dec(label="split", parts=[part(label="steer", text="refactor can wait", note="pause", target="p1"),
                                  part(label="amend", text="actually do the webhook first", target="p2", depends_on=[])])
    out = intake.handle(ev("actually do the webhook first, refactor can wait"), classifier=lambda p: d)
    s = store.load("s1")
    assert s.get("p1").status == "paused" and s.get("p2").depends_on == [] and s.ready()[0].id == "p2"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "paused p1" in ctx and "p2 now depends on nothing" in ctx and "act only on: refactor can wait" in ctx


def test_dedupe_lets_a_plain_prompt_run_again_after_it_finished(data_dir):
    intake.handle(ev("run the tests"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.state = "idle"; s.running_task = None
    assert intake.handle(ev("Run the tests.", prompt_id="pid-2")) is None
    assert store.load("s1").get("p2").status == "running"


def test_dedupe_still_catches_held_marker_text_after_it_finished(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    with store.locked("s1") as s:
        for t in s.tasks: t.status = "done"
        s.state = "idle"; s.running_task = None
    out = intake.handle(ev("later: email verification", prompt_id="pid-2"))
    assert "already p2 (done)" in out["systemMessage"] and store.load("s1").next_id == 3


# --- M2 Task 6: the waiting states ------------------------------------------

from qsc import dispatch, done, rules  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def waiting(state="awaiting_approval"):
    intake.handle(ev("add a user model"))
    with store.locked("s1") as s:
        b = s.new_task("add login", ["p1"], "pid-1"); b.status = "planned"; s.get("p1").status = "done"
        s.state = state; s.pending_task = "p2"; s.running_task = None
    p = paths.plan_path("s1", "p2"); p.parent.mkdir(parents=True, exist_ok=True); p.write_text("# plan p2")


def test_lgtm_approves_and_starts(data_dir):
    waiting()
    out = intake.handle(ev("lgtm", prompt_id="pid-2"))
    assert "decision" not in out and "Execute the plan at" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p2" and s.get("p2").status == "running"


def test_ok_is_approval_only_when_waiting(data_dir):
    waiting()
    assert "Execute the plan" in intake.handle(ev("ok", prompt_id="pid-2"))["hookSpecificOutput"]["additionalContext"]   # review focus 4
    assert intake.handle(ev("ok", prompt_id="pid-3")) is None      # now running: ok is a non-task again


def test_revise_keeps_waiting(data_dir):
    waiting()
    out = intake.handle(ev("looks good but do the rate limiter before the endpoint", prompt_id="pid-2"), classifier=lambda p: dec(label="steer", note="revise plan"))
    assert "revise the plan at" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.state == "awaiting_approval" and s.get("p2").status == "planned"


def test_g46_approve_plus_queue(data_dir):
    waiting()
    d = dec(label="split", parts=[part(label="steer", text="go", note="approve"), part(label="queue", text="add logout", depends_on=["p2"])])
    out = intake.handle(ev("go, and once it's in add logout", prompt_id="pid-2"), classifier=lambda p: d)
    s = store.load("s1"); assert s.get("p3").status == "queued" and s.get("p3").depends_on == ["p2"] and s.state == "running" and s.running_task == "p2"
    assert "Execute the plan" in out["hookSpecificOutput"]["additionalContext"]


def test_unrelated_prompt_defers_approval(data_dir):
    waiting()
    out = intake.handle(ev("what time zone does the server use?", prompt_id="pid-2"), classifier=lambda p: dec(label="steer"))
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p3" and s.get("p2").status == "planned" and s.pending_task is None   # review focus 1


def test_answer_resumes_running_task(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    assert intake.handle(ev("use postgres", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p2" and s.next_id == 3


def test_confirmation_closes_task_and_plans_next(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s:
        s.get("p2").status = "running"; s.new_task("add logout", ["p2"], "pid-1")
    out = intake.handle(ev("ok, merge", prompt_id="pid-2"))
    s = store.load("s1")
    assert s.get("p2").status == "done" and s.get("p2").verdict == "confirmed" and paths.handoff_path("s1", "p2").exists()
    assert "Write a short plan for p3" in out["hookSpecificOutput"]["additionalContext"] and s.state == "planning"


# --- additional Task 6 tests beyond the plan ---------------------------------

def test_approval_never_calls_classifier_and_logs(data_dir):
    waiting()
    called = []
    out = intake.handle(ev("ship it!", prompt_id="pid-2"), classifier=lambda p: called.append(p))
    assert called == [] and "Execute the plan" in out["hookSpecificOutput"]["additionalContext"]
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "approve" and row["task"] == "p2"
    s = store.load("s1"); assert s.current_prompt_id == "pid-2" and s.pending_task is None


def test_classifier_approve_note_approves(data_dir):
    waiting()
    seen = {}
    def clf(p): seen.update(p); return dec(label="steer", note="approve")
    out = intake.handle(ev("yeah do that", prompt_id="pid-2"), classifier=clf)
    assert "Execute the plan" in out["hookSpecificOutput"]["additionalContext"]
    # The classifier saw the waiting state and the pending task as the current one (context E).
    assert seen["state"] == "awaiting_approval" and seen["running_task"]["id"] == "p2"
    assert store.load("s1").state == "running"


def test_low_confidence_approve_note_defers(data_dir):
    waiting()
    intake.handle(ev("yeah do that", prompt_id="pid-2"), classifier=lambda p: dec(label="steer", note="approve", confidence=0.5))
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p3" and s.get("p2").status == "planned"


def test_revise_passes_message_and_names_plan_path(data_dir):
    waiting()
    out = intake.handle(ev("looks good but do the rate limiter before the endpoint", prompt_id="pid-2"), classifier=lambda p: dec(label="steer", note="revise plan"))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in out and str(paths.plan_path("s1", "p2")) in ctx and ctx.startswith("queue-switchclaude:")
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "revise"
    # A Stop after the revised plan is presented changes nothing: the plan still waits.
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "Revised plan presented."}) is None
    assert store.load("s1").state == "awaiting_approval"


def test_queue_marker_while_awaiting_approval_queues_after_pending(data_dir):
    waiting()
    out = intake.handle(ev("later: add logout", prompt_id="pid-2"))
    assert out["decision"] == "block" and "queued p3 after p2" in out["systemMessage"]
    s = store.load("s1"); assert s.state == "awaiting_approval" and s.pending_task == "p2" and s.get("p3").depends_on == ["p2"]
    # The held text coming back is a duplicate, not a second task.
    assert "already p3" in intake.handle(ev("later: add logout", prompt_id="pid-3"))["systemMessage"]


def test_stop_while_waiting_blocks_without_setting_the_flag(data_dir):
    waiting()
    out = intake.handle(ev("stop", prompt_id="pid-2"))
    assert out["decision"] == "block" and "p2" in out["systemMessage"]
    s = store.load("s1"); assert s.interrupt is False and s.state == "awaiting_approval" and s.get("p2").status == "planned"


def test_harness_traffic_and_slash_commands_pass_while_waiting(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    assert intake.handle(ev("<system-reminder>ok</system-reminder>", prompt_id="pid-2")) is None
    assert intake.handle(ev("/compact", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.state == "awaiting_answer" and s.pending_task == "p2" and s.next_id == 3


def test_non_task_word_while_awaiting_approval_is_an_approval(data_dir):
    waiting()
    out = intake.handle(ev("continue", prompt_id="pid-2"))
    assert "Execute the plan at" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p2" and s.next_id == 3


def test_control_approve_hands_over_execution(data_dir):
    waiting()
    out = intake.handle(ev("queue: approve", prompt_id="pid-2"))
    assert "decision" not in out
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "approved p2" in ctx and "Execute the plan at" in ctx
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p2" and s.get("p2").status == "running"


def test_control_show_names_the_plan_file(data_dir):
    waiting()
    out = intake.handle(ev("queue: show p2", prompt_id="pid-2"))
    assert str(paths.plan_path("s1", "p2")) in out["systemMessage"] and store.load("s1").state == "awaiting_approval"


# --- M3 Task 4: harness traffic never touches a waiting store ----------------

def test_harness_traffic_passes_even_while_waiting(data_dir):
    waiting()
    assert intake.handle(ev("<task-notification>x", prompt_id="pid-2")) is None
    assert store.load("s1").state == "awaiting_approval"                                        # review focus 3


def test_harness_traffic_passes_in_every_state_without_a_task(data_dir):
    # Idle: no store is created. Running: no task, no dedupe, same turn. Waiting for an answer: still waiting.
    assert intake.handle(ev("[Request interrupted by user for tool use]")) is None
    assert not paths.store_path("s1").exists()
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("Base directory for this skill: /x\n\n# Skill", prompt_id="pid-2")) is None
    assert intake.handle(ev("<command-message>queue: list</command-message>", prompt_id="pid-3")) is None
    s = store.load("s1"); assert s.next_id == 2 and s.state == "running" and s.current_prompt_id == "pid-1"
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    assert intake.handle(ev("<task-notification>\n<task-id>t</task-id>", prompt_id="pid-4")) is None
    s = store.load("s1"); assert s.state == "awaiting_answer" and s.pending_task == "p2" and s.next_id == 3


def test_control_list_marks_awaiting_approval(data_dir):
    waiting()
    assert "p2 planned (awaiting approval)" in intake.handle(ev("queue: list", prompt_id="pid-2"))["systemMessage"]


def test_deferred_approval_is_represented_after_the_turn(data_dir, monkeypatch):
    waiting()
    with store.locked("s1") as s: s.get("p2").plan_based_on = ["p1"]        # as dispatch records it: p1 was done when the plan was written
    intake.handle(ev("what time zone does the server use?", prompt_id="pid-2"), classifier=lambda p: dec(label="steer"))
    monkeypatch.setattr(done, "judge", lambda *a, **k: done.Verdict("done", "r"))
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "UTC.", "cwd": str(ROOT)})
    assert "p2" in out["hookSpecificOutput"]["additionalContext"] and "waiting for your approval" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.state == "awaiting_approval" and s.pending_task == "p2" and s.get("p3").status == "done"
    assert "Execute the plan" in intake.handle(ev("lgtm", prompt_id="pid-3"))["hookSpecificOutput"]["additionalContext"]


def test_answer_with_but_clause_is_an_answer_not_a_confirmation(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    assert intake.handle(ev("ok, but use postgres", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p2" and s.get("p2").status == "running"
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "answer" and row["task"] == "p2"


def test_answer_sets_prompt_id_so_later_messages_are_mid_turn(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    intake.handle(ev("use postgres", prompt_id="pid-2"))
    out = intake.handle(ev("later: add logout", prompt_id="pid-2"))
    assert "queued p3 after p2" in out["systemMessage"] and store.load("s1").state == "running"


def test_confirmation_without_next_task_starts_a_turn_with_notice(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    out = intake.handle(ev("ok", prompt_id="pid-2"))
    assert "decision" not in out and "p2 confirmed done" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1")
    assert s.get("p2").status == "done" and s.get("p2").verdict == "confirmed" and s.state == "running" and s.running_task == "p3"
    assert "- user confirmed" not in paths.handoff_path("s1", "p2").read_text() and "confirmed: user confirmed" in paths.handoff_path("s1", "p2").read_text()


def test_queue_marker_while_awaiting_answer_queues_and_keeps_waiting(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    out = intake.handle(ev("later: add logout", prompt_id="pid-2"))
    assert "queued p3 after p2" in out["systemMessage"]
    s = store.load("s1"); assert s.state == "awaiting_answer" and s.pending_task == "p2"


def test_waiting_state_without_pending_task_fails_open_to_a_new_turn(data_dir):
    waiting()
    with store.locked("s1") as s: s.pending_task = None
    assert intake.handle(ev("add logout", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p3"


def test_split_queue_only_while_waiting_blocks_and_keeps_waiting(data_dir):
    waiting()
    d = dec(label="split", parts=[part(label="queue", text="add logout", depends_on=["p2"]), part(label="queue", text="add profile", depends_on=["p3"])])
    out = intake.handle(ev("1) add logout 2) add profile", prompt_id="pid-2"), classifier=lambda p: d)
    assert out["decision"] == "block" and "queued p4 after p3" in out["systemMessage"]
    s = store.load("s1"); assert s.state == "awaiting_approval" and s.get("p2").status == "planned"


def test_queue_marker_during_planning_turn_is_mid_turn(data_dir):
    intake.handle(ev("add a user model"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; b = s.new_task("add login", ["p1"], "pid-1")
        s.state = "planning"; s.pending_task = "p2"; s.running_task = None
    out = intake.handle(ev("later: add logout"))                      # same prompt_id: the planning continuation
    assert "queued p3 after p2" in out["systemMessage"]
    s = store.load("s1"); assert s.state == "planning" and s.pending_task == "p2" and s.get("p2").status == "queued"


def test_new_turn_during_planning_clears_pending_without_esc_drop(data_dir):
    intake.handle(ev("add a user model"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.new_task("add login", ["p1"], "pid-1"); s.new_task("add logout", ["p2"], "pid-1")
        s.state = "planning"; s.pending_task = "p2"; s.running_task = None
    # M3 Task 1: a new prompt_id during planning is Esc on the planning turn
    # (review focus 2): the pending task is queued again and the prompt
    # carries a notice; nothing is dropped.
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    assert "planning turn for p2 was interrupted" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1")
    assert s.state == "running" and s.running_task == "p4" and s.pending_task is None
    assert s.get("p2").status == "queued" and s.get("p3").status == "queued"


# --- M3 Task 1: idle-state markers and Esc during planning ------------------

def test_idle_queue_marker_runs_now_with_stripped_text(data_dir):
    out = intake.handle(ev("later: add goodbye.py"))
    assert "starts now: add goodbye.py" in out["hookSpecificOutput"]["additionalContext"]
    t = store.load("s1").get("p1"); assert t.text == "add goodbye.py" and t.status == "running"


def test_idle_parked_marker_parks_and_blocks(data_dir):
    out = intake.handle(ev("maybe later we can add a README"))
    assert out["decision"] == "block" and "parked p1" in out["systemMessage"]
    s = store.load("s1"); assert s.get("p1").status == "parked" and s.state == "idle"


def test_idle_stop_is_a_noop(data_dir):
    out = intake.handle(ev("stop"))
    assert out["decision"] == "block" and "nothing is running" in out["systemMessage"]
    assert not store.load("s1").tasks and not store.load("s1").interrupt


def test_esc_during_planning_requeues_pending(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: add login"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.state = "planning"; s.pending_task = "p2"; s.running_task = None
    p = paths.plan_path("s1", "p2"); p.parent.mkdir(parents=True, exist_ok=True); p.write_text("# half")
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    s = store.load("s1")
    assert s.get("p2").status == "queued" and s.pending_task is None and not p.exists()
    assert s.running_task == "p3" and s.state == "running"
    assert "planning turn for p2 was interrupted" in out["hookSpecificOutput"]["additionalContext"]     # review focus 2


# --- additional Task 1 tests beyond the plan ---------------------------------

def test_idle_queue_marker_passes_through_and_logs_marker_stripped(data_dir):
    out = intake.handle(ev("later: add goodbye.py"))
    assert "decision" not in out and out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p1" and s.current_prompt_id == "pid-1"
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "new_turn" and row["marker_stripped"] is True and row["task"] == "p1"
    # The turn is the user's own: a marker typed during it queues after p1 as usual.
    assert "queued p2 after p1" in intake.handle(ev("later: add hello.py"))["systemMessage"]


def test_idle_queue_marker_in_shadow_keeps_the_raw_prompt(data_dir):
    assert intake.handle(ev("later: add goodbye.py"), shadow=True) is None
    t = store.load("s1").get("p1"); assert t.text == "later: add goodbye.py" and t.status == "running"
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "new_turn" and row["marker_stripped"] is False


def test_idle_parked_marker_logs_parked_idle_and_never_dispatches(data_dir):
    intake.handle(ev("maybe later we can add a README"))
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "parked_idle" and row["task"] == "p1" and row["parked"] is True
    s = store.load("s1"); assert s.ready() == [] and s.running_task is None and s.pending_task is None
    # The next prompt is an ordinary turn that does not chain after the parked item.
    assert intake.handle(ev("add hello.py", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.running_task == "p2" and s.get("p2").depends_on == []


def test_idle_parked_marker_in_shadow_records_and_passes(data_dir):
    assert intake.handle(ev("maybe later we can add a README"), shadow=True) is None
    s = store.load("s1")
    assert s.get("p1").status == "shadow_queued" and s.state == "running" and s.running_task == "p2"


def test_idle_parked_marker_without_content_is_a_plain_turn(data_dir):
    assert intake.handle(ev("maybe later we can add this")) is None
    t = store.load("s1").get("p1"); assert t.status == "running" and t.text == "maybe later we can add this"


def test_idle_stop_logs_noop_and_the_next_prompt_runs(data_dir):
    intake.handle(ev("stop"))
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "noop" and row["label"] == "interrupt" and row["state"] == "idle"
    assert intake.handle(ev("add hello.py", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.running_task == "p1" and s.interrupt is False


def test_idle_stop_in_shadow_is_a_turn(data_dir):
    assert intake.handle(ev("stop"), shadow=True) is None
    s = store.load("s1"); assert s.get("p1").status == "running" and s.interrupt is False


def test_idle_stop_after_a_finished_turn_is_a_noop(data_dir):
    intake.handle(ev("add a user model"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.state = "idle"; s.running_task = None
    out = intake.handle(ev("stop", prompt_id="pid-2"))
    assert out["decision"] == "block" and "nothing is running" in out["systemMessage"]
    s = store.load("s1"); assert s.state == "idle" and s.next_id == 2 and s.interrupt is False


def test_idle_markers_still_dedupe_held_text_first(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: add goodbye.py"))
    with store.locked("s1") as s:
        for t in s.tasks: t.status = "done"
        s.state = "idle"; s.running_task = None
    out = intake.handle(ev("later: add goodbye.py", prompt_id="pid-2"))
    assert out["decision"] == "block" and "already p2 (done)" in out["systemMessage"] and store.load("s1").next_id == 3


def test_esc_during_planning_keeps_other_queued_tasks_and_logs(data_dir):
    intake.handle(ev("add a user model"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.new_task("add login", ["p1"], "pid-1"); s.new_task("add logout", ["p2"], "pid-1")
        s.state = "planning"; s.pending_task = "p2"; s.running_task = None; s.get("p2").attempts = 1
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    s = store.load("s1")
    assert s.get("p2").status == "queued" and s.get("p2").attempts == 0 and s.get("p3").status == "queued"
    assert s.state == "running" and s.running_task == "p4" and s.pending_task is None
    assert out["hookSpecificOutput"]["additionalContext"] == "queue-switchclaude: the planning turn for p2 was interrupted; it is queued again"
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["decision"] == "new_turn" and row["interrupted_planning"] == "p2" and row["dropped"] == [] and row["interrupted"] is None


def test_esc_during_planning_then_the_task_is_planned_again(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: add login"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.state = "planning"; s.pending_task = "p2"; s.running_task = None
    intake.handle(ev("what time is it", prompt_id="pid-2"))
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "noon", "cwd": str(ROOT)})
    assert "Write a short plan for p2" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.state == "planning" and s.pending_task == "p2" and s.get("p3").status == "done"


def test_esc_during_planning_does_not_resurrect_a_cancelled_task(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: add login"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.state = "planning"; s.pending_task = "p2"; s.running_task = None
        s.get("p2").status = "cancelled"
    assert intake.handle(ev("something else", prompt_id="pid-2")) is None
    s = store.load("s1"); assert s.get("p2").status == "cancelled" and s.pending_task is None and s.running_task == "p3"


# --- review follow-ups after M3 -------------------------------------------------

def test_idle_later_with_paused_queue_queues_after_it(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.state = "idle"; s.running_task = None; s.paused = True
    out = intake.handle(ev("later: add logout", prompt_id="pid-2"))
    assert out["decision"] == "block" and "queued p3 after p2" in out["systemMessage"] and "nothing is running" in out["systemMessage"]
    s = store.load("s1"); assert s.get("p3").status == "queued" and s.get("p3").depends_on == ["p2"] and s.running_task is None


def test_idle_later_with_only_parked_work_runs_now(data_dir):
    intake.handle(ev("maybe later we can add a README"))
    out = intake.handle(ev("later: add goodbye.py", prompt_id="pid-2"))
    assert "starts now: add goodbye.py" in out["hookSpecificOutput"]["additionalContext"]


def test_stop_right_after_esc_is_a_noop_with_the_esc_notice(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests"))
    out = intake.handle(ev("stop", prompt_id="pid-2"))                    # new prompt_id while running: Esc, then stop
    assert out["decision"] == "block" and "nothing is running" in out["systemMessage"] and "dropped queued tasks: p2" in out["systemMessage"]
    s = store.load("s1"); assert s.get("p1").status == "interrupted" and s.get("p2").status == "dropped" and s.next_id == 3 and not s.interrupt


def test_later_right_after_esc_runs_with_marker_stripped(data_dir):
    intake.handle(ev("add a user model"))
    out = intake.handle(ev("later: add goodbye.py", prompt_id="pid-2"))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "starts now: add goodbye.py" in ctx
    s = store.load("s1"); assert s.get("p1").status == "interrupted" and s.get("p2").text == "add goodbye.py" and s.get("p2").status == "running"


# --- M4 Task 1: visible notices ----------------------------------------------

def test_idle_run_now_shows_a_message(data_dir):
    out = intake.handle(ev("later: add goodbye.py"))
    assert out["systemMessage"] == "queue-switchclaude: nothing was queued, running now: add goodbye.py"
    assert out["hookSpecificOutput"]["additionalContext"].endswith("starts now: add goodbye.py")   # review focus 1


def test_esc_drop_shows_a_message(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: a")); intake.handle(ev("later: b"))
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    assert out["systemMessage"] == "queue-switchclaude: Esc dropped p2, p3"


def test_approval_shows_a_message(data_dir):
    waiting()
    assert intake.handle(ev("lgtm", prompt_id="pid-2"))["systemMessage"] == "queue-switchclaude: running p2"


def test_split_shows_the_confirmations_only(data_dir):
    intake.handle(ev("add a user model"))
    d = dec(label="split", parts=[part(label="steer", text="use argon2 instead"), part(label="queue", text="add password reset", depends_on=["p1"])])
    out = intake.handle(ev("use argon2 instead, and after that add password reset"), classifier=lambda p: d)
    assert out["systemMessage"] == "queue-switchclaude: queued p2 after p1: add password reset"


def test_answer_has_no_message(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    assert intake.handle(ev("use postgres", prompt_id="pid-2")) is None


# --- additional Task 1 tests beyond the plan ---------------------------------

def test_notices_keep_additional_context_byte_identical(data_dir):
    # Review focus 1: the systemMessage sits next to the context, never inside it.
    out = intake.handle(ev("later: add goodbye.py"))
    assert out["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit",
                                         "additionalContext": "queue-switchclaude: nothing is running, so this starts now: add goodbye.py"}
    assert set(out) == {"hookSpecificOutput", "systemMessage"} and "\n" not in out["systemMessage"]


def test_esc_drop_context_is_unchanged_by_the_message(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: a")); intake.handle(ev("later: b"))
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    assert out["hookSpecificOutput"]["additionalContext"] == \
        "queue-switchclaude: the previous turn was interrupted, dropped queued tasks: p2 (a), p3 (b)"


def test_esc_with_nothing_queued_shows_nothing(data_dir):
    intake.handle(ev("add a user model"))
    assert intake.handle(ev("something else", prompt_id="pid-2")) is None


def test_esc_during_planning_shows_a_message(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: add login"))
    with store.locked("s1") as s:
        s.get("p1").status = "done"; s.state = "planning"; s.pending_task = "p2"; s.running_task = None
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    assert out["systemMessage"] == "queue-switchclaude: p2 is queued again"
    assert out["hookSpecificOutput"]["additionalContext"] == "queue-switchclaude: the planning turn for p2 was interrupted; it is queued again"


def test_esc_then_idle_later_shows_both_on_one_line(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests"))
    out = intake.handle(ev("later: add goodbye.py", prompt_id="pid-2"))
    assert out["systemMessage"] == "queue-switchclaude: Esc dropped p2; nothing was queued, running now: add goodbye.py"
    assert out["hookSpecificOutput"]["additionalContext"] == (
        "queue-switchclaude: the previous turn was interrupted, dropped queued tasks: p2 (write tests); "
        "queue-switchclaude: nothing is running, so this starts now: add goodbye.py")


def test_control_approve_shows_running(data_dir):
    waiting()
    out = intake.handle(ev("queue: approve", prompt_id="pid-2"))
    assert out["systemMessage"] == "queue-switchclaude: running p2" and "Execute the plan at" in out["hookSpecificOutput"]["additionalContext"]


def test_continue_approval_shows_running(data_dir):
    waiting()
    assert intake.handle(ev("continue", prompt_id="pid-2"))["systemMessage"] == "queue-switchclaude: running p2"


def test_revise_shows_a_message(data_dir):
    waiting()
    out = intake.handle(ev("looks good but do the rate limiter before the endpoint", prompt_id="pid-2"), classifier=lambda p: dec(label="steer", note="revise plan"))
    assert out["systemMessage"] == "queue-switchclaude: revising the plan for p2"
    assert out["hookSpecificOutput"]["additionalContext"].startswith("queue-switchclaude: revise the plan at")


def test_confirmation_shows_done_with_and_without_a_next_task(data_dir):
    waiting("awaiting_answer")
    with store.locked("s1") as s: s.get("p2").status = "running"
    out = intake.handle(ev("ok", prompt_id="pid-2"))
    assert out["systemMessage"] == "queue-switchclaude: p2 confirmed done"
    waiting("awaiting_answer")
    with store.locked("s1") as s:
        s.get("p2").status = "running"; s.new_task("add logout", ["p2"], "pid-1")
    out = intake.handle(ev("ok, merge", prompt_id="pid-3"))
    assert out["systemMessage"] == "queue-switchclaude: p2 confirmed done"
    assert "Write a short plan for" in out["hookSpecificOutput"]["additionalContext"]


def test_g46_approve_plus_queue_shows_both(data_dir):
    waiting()
    d = dec(label="split", parts=[part(label="steer", text="go", note="approve"), part(label="queue", text="add logout", depends_on=["p2"])])
    out = intake.handle(ev("go, and once it's in add logout", prompt_id="pid-2"), classifier=lambda p: d)
    assert out["systemMessage"] == "queue-switchclaude: running p2; queued p3 after p2: add logout"


def test_split_while_waiting_with_steer_text_shows_the_confirmations(data_dir):
    waiting()
    d = dec(label="split", parts=[part(label="steer", text="what time zone is it"), part(label="queue", text="add logout", depends_on=["p2"])])
    out = intake.handle(ev("what time zone is it, and add logout", prompt_id="pid-2"), classifier=lambda p: d)
    assert out["systemMessage"] == "queue-switchclaude: queued p3 after p2: add logout"
    assert out["hookSpecificOutput"]["additionalContext"].endswith("act only on: what time zone is it")


def test_split_with_amend_and_steer_shows_the_confirmation(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: write tests for login"))
    d = dec(label="split", parts=[part(label="steer", text="use argon2"), part(label="amend", text="skip the tests", target="p2", note="cancel")])
    out = intake.handle(ev("use argon2, and skip the tests"), classifier=lambda p: d)
    assert out["systemMessage"] == "queue-switchclaude: cancelled p2: write tests for login"


def test_pause_pass_through_shows_the_confirmation(data_dir):
    intake.handle(ev("refactor billing"))
    out = intake.handle(ev("hold the refactor, something came up"), classifier=lambda p: dec(label="steer", note="pause", target="p1"))
    assert out["systemMessage"] == "queue-switchclaude: paused p1 (send 'queue: resume p1' to run it again)"


def test_shadow_pass_throughs_show_nothing(data_dir):
    # Shadow mode changes nothing on screen: the Esc notice still reaches Claude
    # as context (unchanged), but the dropped tasks were never shown as queued.
    assert intake.handle(ev("later: add goodbye.py"), shadow=True) is None
    intake.handle(ev("later: a"), shadow=True)
    out = intake.handle(ev("something else", prompt_id="pid-2"), shadow=True)
    assert "systemMessage" not in out and "dropped queued tasks: p2 (a)" in out["hookSpecificOutput"]["additionalContext"]


def test_dispatch_lines_logged_from_intake_carry_hook_ms(data_dir):
    # A confirmation runs dispatch.write_handoff and dispatch.choose_next inside
    # the intake process; their log lines must count from the same start stamp
    # instead of reading 0 (M4 review: dispatch._record saw no event of its own).
    import time
    waiting("awaiting_answer")
    with store.locked("s1") as s:
        s.get("p2").status = "running"; s.new_task("add logout", ["p2"], "pid-1")
    e = ev("ok, merge", prompt_id="pid-3"); e["_hook_started"] = time.monotonic() - 0.05
    intake.handle(e)
    rows = [json.loads(line) for line in paths.log_path("s1").read_text().splitlines()]
    from_dispatch = [r for r in rows if r["event"] in ("dispatch", "handoff")]
    assert len(from_dispatch) == 2 and all(r["hook_ms"] >= 50 for r in from_dispatch), from_dispatch
