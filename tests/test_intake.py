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
    intake.handle(ev("stop"))                       # a new turn whose text happens to be "stop"
    out = intake.handle(ev("stop"))                 # mid-turn: this is an interrupt, not a duplicate
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
