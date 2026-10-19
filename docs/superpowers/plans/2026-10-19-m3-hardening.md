# M3 Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every known rough edge from the M0 to M2 reviews and acceptance runs so the plugin can be installed from GitHub and left on in daily sessions without surprises.

**Architecture:** No new subsystems. Each task tightens one existing module: intake's idle-state handling and Esc detection during planning, the multi-session pointer and session garbage collection, a spin guard that can actually fire, harness-traffic patterns, the interrupt hook's cost, and the packaging (marketplace manifest, CI, version). Every change keeps the fail-open rule.

**Tech Stack:** Python 3.10+ stdlib only under `scripts/`, `pytest`, GitHub Actions for CI, Claude Code plugin marketplace manifest.

**Spec:** `docs/design-spec.md` §10 M3, §6; `docs/decisions.md` D5; findings in `docs/m1-acceptance.md`, `docs/m2-acceptance.md`, `docs/m2-fixes-acceptance.md` and the review summaries recorded in the M1 and M2 workflow runs.

## Global Constraints

- Everything under `scripts/` stays Python 3.10+ stdlib only; hook scripts exit 0 silently on any error.
- No change to the hook output shapes or to the classifier contract (`classify.SCHEMA`) in this milestone; the eval gate must still pass unchanged (`python3 eval/run_golden.py` from cache: 44 of 47, zero missed hard dependencies).
- Store files from M0 to M2 keep loading; new fields have defaults.
- A session's files are never touched by another session's hooks except through the pointer files, which are written atomically.
- Plugin version becomes `0.3.0` in `.claude-plugin/plugin.json`.

## Review Focus

1. Two Claude Code sessions in different folders, both with the plugin: `/queue-switchclaude:queue list` in one must never show the other's queue. Test in Task 2.
2. Pressing Esc during a planning turn, then typing something new: the half-planned task must return to `queued` and the new prompt must run as a normal turn, with no `pending_task` left behind. Test in Task 1.
3. A harness-generated prompt (a task notification, a skill invocation, an interrupted-request marker) must never become a task or a dedupe hit, in any state. Test in Task 4.
4. Garbage collection must never delete the session that is running, or any session file younger than the retention window, even if the clock is wrong by minutes. Test in Task 2.
5. The marketplace install must work on a fresh machine: `claude plugin validate` on the repo root passes with the marketplace manifest, and the README install line names the real repository. Test in Task 6.

---

### Task 1: Idle-state markers and Esc during planning

**Files:**
- Modify: `scripts/qsc/intake.py`
- Test: `tests/test_intake.py` (append)

**Interfaces:**
- Produces, in `handle` before `_new_turn` when the state is `idle` (nothing running, nothing pending):
  - `queue_marker` verdict → the remainder (`v.text`), not the raw prompt, becomes the running task's text and the message passes through with `additionalContext = "queue-switchclaude: nothing is running, so this starts now: {text}"`; log `decision="new_turn", marker_stripped=True`.
  - `parked_marker` verdict → the task is created `parked` and the message is blocked with the parked confirmation (nothing runs); log `decision="parked_idle"`.
  - `interrupt` verdict (`stop` at idle) → blocked with `"queue-switchclaude: nothing is running"`; no task, no flag; log `decision="noop"`.
- Produces, in `_new_turn` when `s.state == "planning"` (a new `prompt_id` arrived while Claude was writing a plan): the pending task's status returns to `queued`, its partial plan file is deleted, `s.pending_task = None`, log `interrupted_planning=<id>`, and the notice attached to the prompt is `"queue-switchclaude: the planning turn for {id} was interrupted; it is queued again"`. Queued tasks are not dropped in this case: the user interrupted planning, not work (D5 applies to a running task).

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_intake.py -v -k "idle_ or esc_during_planning"`
Expected: FAIL

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/intake.py tests/test_intake.py
git commit -m "feat(intake): idle markers run, park or no-op; Esc during planning requeues the task"
```

---

### Task 2: Multi-session hygiene

**Files:**
- Modify: `scripts/qsc/paths.py`, `scripts/qsc/intake.py` (pointer write), `scripts/queue_cli.py`, `scripts/qsc/dispatch.py` (gc call)
- Create: `scripts/qsc/gc.py`
- Test: `tests/test_paths.py`, `tests/test_queue_cli.py`, `tests/test_gc.py`

**Interfaces:**
- Produces: `paths.cwd_pointer_path(cwd: str) -> Path` = `data_dir()/pointers/<sha1(cwd)[:16]>`; intake writes both `pointer_path()` (newest session anywhere) and the cwd pointer on every event, from `event["cwd"]`.
- Produces: `queue_cli.resolve_session(explicit, cwd=os.getcwd())`: `--session`, else `$QSC_SESSION`, else the cwd pointer for `cwd`, else the global pointer, else `None`. The command file is unchanged (the Bash tool runs in the project directory).
- Produces: `gc.collect(keep_days: int = 30, now: float | None = None, protect: set[str] = frozenset()) -> list[str]`: deletes session stores, logs and session directories whose newest file is older than `keep_days`, never a session in `protect`, never anything younger than the window; returns the deleted session ids. Dispatch calls `gc.collect(protect={session_id})` at most once per session (a `gc_at` timestamp in the store) and swallows every error.
- `Store` gains `gc_at: str | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cwd_pointer_isolates_sessions(data_dir):
    intake.handle({**ev("a", sid="s-one"), "cwd": "/proj/one"}); intake.handle({**ev("b", sid="s-two"), "cwd": "/proj/two"})
    assert queue_cli.resolve_session(None, cwd="/proj/one") == "s-one" and queue_cli.resolve_session(None, cwd="/proj/two") == "s-two"   # review focus 1
    assert queue_cli.resolve_session(None, cwd="/proj/none") == "s-two"      # global pointer: newest

def test_gc_deletes_old_keeps_young_and_protected(data_dir):
    for sid, age_days in (("old", 40), ("young", 3), ("me", 400)):
        s = store.load(sid); store.save(s); log.record(sid, "x")
        for p in (paths.store_path(sid), paths.log_path(sid)):
            t = time.time() - age_days * 86400; os.utime(p, (t, t))
    deleted = gc.collect(keep_days=30, protect={"me"})
    assert deleted == ["old"] and paths.store_path("young").exists() and paths.store_path("me").exists()   # review focus 4

def test_gc_runs_once_per_session_from_dispatch(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(gc, "collect", lambda **kw: (calls.append(kw), [])[1])
    intake.handle(ev("a")); dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"})
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"})
    assert len(calls) == 1 and calls[0]["protect"] == {"s1"} and store.load("s1").gc_at
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_paths.py tests/test_queue_cli.py tests/test_gc.py -v -k "cwd_pointer or gc_"`
Expected: FAIL

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/paths.py scripts/qsc/intake.py scripts/qsc/dispatch.py scripts/qsc/gc.py scripts/qsc/store.py scripts/queue_cli.py tests
git commit -m "feat: per-cwd session pointer and session garbage collection"
```

---

### Task 3: Dispatch and control polish

**Files:**
- Modify: `scripts/qsc/dispatch.py`, `scripts/qsc/control.py`, `scripts/qsc/done.py`, `scripts/qsc/store.py`
- Test: `tests/test_dispatch.py`, `tests/test_control.py`, `tests/test_done.py` (append)

**Interfaces:**
- Produces: a spin guard that can fire. `Store` gains `last_handback: str | None` (sha1 of the last handover text issued). In `dispatch.handle`, when `stop_hook_active` is true and the handover text about to be returned hashes equal to `last_handback`, it is suppressed: log `dispatch` with `spin_guard=True`, return `{"systemMessage": "queue-switchclaude: the same instruction came back twice; stopping here, send any message to continue"}`. The M1 `last_dispatched` guard is removed (it could never fire).
- Produces: the execution handover for an `auto` task after its planning turn says `"...Execute the plan at {path} now if you have not already carried it out; if you have, say so and stop."`
- Produces: `_verify_failed_text` omits `Output tail:` when the tail is empty.
- Produces: `control._cancel` failure reply `"{id} is not queued, parked, paused, planned or failed_verify"`; `_promote` failure reply `"{id} is not parked, paused or failed_verify"`. In shadow mode `cancel`, `park` and `drop` also act on `shadow_queued` tasks (so the control channel can edit what shadow mode recorded).
- Produces: `done.VERDICTS` gains `confirmed`; `done.run_verify` starts the shell with `start_new_session=True` and on timeout kills the process group (`os.killpg`) so a verify that spawned a server does not outlive the hook.

- [ ] **Step 1: Write the failing tests**

```python
def test_spin_guard_fires_on_identical_handback(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")                     # idle store, one ready task
    first = dispatch.handle(stop(active=True))["hookSpecificOutput"]["additionalContext"]     # planning handover
    assert "Write a short plan for p1" in first
    out = dispatch.handle(stop(active=True))         # planning, no plan file: the retry text is identical
    assert "hookSpecificOutput" not in out and "came back twice" in out["systemMessage"]
    assert store.load("s1").state == "planning"      # Task 1's Esc-during-planning path requeues it on the next prompt

def test_auto_execution_text_is_idempotent(data_dir):
    with store.locked("s1") as s:
        b = s.new_task("b", [], "pid"); b.auto = True; s.state = "planning"; s.pending_task = "p1"
    p = paths.plan_path("s1", "p1"); p.parent.mkdir(parents=True); p.write_text("# plan")
    assert "if you have not already carried it out" in dispatch.handle(stop(active=True))["hookSpecificOutput"]["additionalContext"]

def test_verify_failed_text_without_tail():
    t = store.Task(id="p1", text="x"); v = done.Verdict("verify_failed", "exit 1", "")
    assert "Output tail" not in dispatch._verify_failed_text(t, v)

def test_control_replies_name_every_status():
    s = seeded(); s.get("p2").status = "done"
    assert control.run(s, "cancel p2") == "p2 is not queued, parked, paused, planned or failed_verify"
    assert control.run(s, "promote p2") == "p2 is not parked, paused or failed_verify"

def test_shadow_control_edits_shadow_tasks():
    s = seeded(); s.get("p2").status = "shadow_queued"
    assert control.run(s, "cancel p2").startswith("cancelled p2")

def test_run_verify_kills_process_group(tmp_path):
    marker = tmp_path / "alive"
    cmd = f"(sleep 30; touch {marker}) & sleep 30"
    v = done.run_verify(cmd, str(tmp_path), timeout=0.5)
    time.sleep(1.0)
    assert v.verdict == "verify_failed" and not marker.exists()
    assert "confirmed" in done.VERDICTS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_dispatch.py tests/test_control.py tests/test_done.py -v -k "spin_guard or idempotent or without_tail or name_every or shadow_control or process_group"`
Expected: FAIL (the old `test_dispatch_spin_guard` tests that construct `last_dispatched` are deleted in Step 3)

- [ ] **Step 3: Implement; delete the M1 spin-guard tests that construct `last_dispatched` (`test_dispatch_spin_guard`, `test_dispatch_spin_guard_only_when_stop_hook_active`) and remove the `last_dispatched` field's use (keep the field for old store files)**

- [ ] **Step 4: Run the suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc tests
git commit -m "fix: spin guard on identical handbacks, idempotent auto handover, verify process group, control reply strings"
```

---

### Task 4: Harness traffic and the interrupt hook's cost

**Files:**
- Modify: `scripts/qsc/rules.py`, `hooks/hooks.json`, `scripts/qsc/interrupt.py`
- Test: `tests/test_rules.py`, `tests/test_intake.py`, `tests/test_packaging.py`, `tests/test_interrupt.py` (append)

**Interfaces:**
- Produces: `rules.HARNESS_PHRASES` gains `"[Request interrupted by user"`, `"<task-notification>"`, `"<side-sessions-event>"`, `"<ci-monitor-event>"`, `"<command-message>"`, `"<command-name>"`, `"Base directory for this skill:"`; a message is harness traffic when it starts with `<`, or contains any phrase in its first 200 characters. `harness_traffic` keeps winning over every other rule, including `control` and the waiting-state checks (intake must not consult the store for it).
- Produces: `hooks/hooks.json` registers `interrupt.py` on `PreToolUse` only. A `PostToolUse` boundary is immediately followed by the next `PreToolUse` or by `Stop`, and `dispatch` already honours the flag, so nothing is lost and every tool call costs one Python launch instead of two.
- Produces: `interrupt.handle` returns `None` without reading the store when the event's `tool_name` is `AskUserQuestion` (the turn is already waiting on the user).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("msg", [
    "[Request interrupted by user for tool use]",
    "<task-notification>\n<task-id>x</task-id>",
    "Base directory for this skill: /x/y\n\n# Brainstorming",
    "<command-message>foo</command-message>\n<command-name>foo</command-name>",
])
def test_more_harness_traffic_passes(msg):
    assert rules.decide(msg).rule == "harness_traffic"

def test_harness_traffic_passes_even_while_waiting(data_dir):
    waiting()
    assert intake.handle(ev("<task-notification>x", prompt_id="pid-2")) is None
    assert store.load("s1").state == "awaiting_approval"                                        # review focus 3

def test_interrupt_hook_registered_on_pretooluse_only():
    h = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    assert "PostToolUse" not in h and h["PreToolUse"][0]["hooks"][0]["args"][0].endswith("interrupt.py")

def test_interrupt_skips_ask_user_question(data_dir):
    with store.locked("s1") as s:
        s.interrupt = True
    assert interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "AskUserQuestion"}) is None
    assert store.load("s1").interrupt is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_rules.py tests/test_intake.py tests/test_packaging.py tests/test_interrupt.py -v -k "harness or pretooluse_only or ask_user"`
Expected: FAIL

- [ ] **Step 3: Implement; update the M0 packaging test that expects a PostToolUse entry**

- [ ] **Step 4: Run the suite and the validator**

Run: `pytest -q && claude plugin validate .`
Expected: all pass; `Validation passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/rules.py scripts/qsc/interrupt.py hooks/hooks.json tests
git commit -m "feat: more harness-traffic patterns; interrupt hook on PreToolUse only"
```

---

### Task 5: Latency and usage report

**Files:**
- Modify: `eval/shadow_report.py`
- Test: `tests/test_shadow_report.py` (append)

**Interfaces:**
- Produces: `hookio.read_event` stamps `event["_hook_started"] = time.monotonic()` when it parses stdin; every `log.record` call in `intake.handle` and `dispatch.handle` adds `hook_ms = round((time.monotonic() - event["_hook_started"]) * 1000)` (0 when the key is absent, as in unit tests that build events by hand). The report adds a section `hook latency`: for each `intake` line, `latency_ms` (already logged for classifier calls) plus `hook_ms`; the report prints p50 and p95 for intake and dispatch, split by `classifier present` versus not. Also counts per `decision` value (`new_turn`, `control`, `dedupe`, `approve`, `revise`, `defer_approval`, `answer`, `confirm`, `parked_idle`, `noop`).

- [ ] **Step 1: Write the failing test**

```python
def test_report_latency_and_decisions(data_dir):
    log.record("s1", "intake", decision="new_turn", hook_ms=40)
    log.record("s1", "intake", label="queue", rule="queue_marker", hook_ms=90)
    log.record("s1", "intake", label="steer", classifier={"label": "steer"}, latency_ms=4000, hook_ms=4100)
    log.record("s1", "dispatch", hook_ms=30)
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "shadow_report.py")], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert "intake p50" in r.stdout and "dispatch p50" in r.stdout and "new_turn: 1" in r.stdout
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_shadow_report.py -v -k latency_and_decisions`
Expected: FAIL

- [ ] **Step 3: Implement (`hook_ms` in the three entry scripts' `main`, passed through the event; the report sections)**

- [ ] **Step 4: Run the suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add scripts eval/shadow_report.py tests/test_shadow_report.py
git commit -m "feat(report): hook latency percentiles and decision counts"
```

---

### Task 6: Installable from GitHub, CI, version

**Files:**
- Create: `.claude-plugin/marketplace.json`
- Modify: `.claude-plugin/plugin.json` (version `0.3.0`, `repository`, `homepage`, `license`), `README.md` (install section), `LICENSE` (MIT, create)
- Create: `.github/workflows/test.yml`
- Test: `tests/test_packaging.py` (append)

**Interfaces:**
- Produces: `.claude-plugin/marketplace.json`:

```json
{
  "name": "queue-switchclaude",
  "owner": {"name": "0xEl1yahu"},
  "plugins": [
    {"name": "queue-switchclaude", "source": ".", "description": "Sorts messages typed mid-run into steer / queue / amend / split, runs queued tasks after the current one with plans and approvals, and stops the turn when you say stop."}
  ]
}
```

- README gains, at the top of "Install":

```bash
/plugin marketplace add 0xEl1yahu/queue-switchclaude
/plugin install queue-switchclaude@queue-switchclaude
```

with the `--plugin-dir` line kept under "Install for development".
- `.github/workflows/test.yml`: on push and pull request, a matrix of Python 3.10, 3.12 and 3.14 on `ubuntu-latest` running `python -m pip install pytest && python -m pytest -q`; no Claude CLI on CI, so the tests that shell out to `claude` already use `tests/fakes/claude`.

- [ ] **Step 1: Write the failing tests**

```python
def test_marketplace_manifest_lists_this_plugin():
    m = json.load(open(ROOT / ".claude-plugin" / "marketplace.json"))
    assert m["name"] == "queue-switchclaude" and m["plugins"][0]["source"] == "." and m["plugins"][0]["name"] == "queue-switchclaude"

def test_plugin_version_and_links():
    m = json.load(open(ROOT / ".claude-plugin" / "plugin.json"))
    assert m["version"] == "0.3.0" and m["repository"].startswith("https://github.com/0xEl1yahu/queue-switchclaude") and m["license"] == "MIT"

def test_readme_has_marketplace_install_line():
    text = (ROOT / "README.md").read_text()
    assert "/plugin marketplace add 0xEl1yahu/queue-switchclaude" in text and (ROOT / "LICENSE").exists() and (ROOT / ".github" / "workflows" / "test.yml").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_packaging.py -v -k "marketplace or version_and_links or readme_has"`
Expected: FAIL

- [ ] **Step 3: Create the files**

- [ ] **Step 4: Run the suite and the validator; it must no longer advise an install line**

Run: `pytest -q && claude plugin validate .`
Expected: all pass; `Validation passed` with no README advice line (review focus 5)

- [ ] **Step 5: Commit**

```bash
git add .claude-plugin README.md LICENSE .github tests/test_packaging.py
git commit -m "chore: marketplace manifest, GitHub install line, MIT licence, CI matrix, version 0.3.0"
```

---

### Task 7: Docs and decisions

**Files:**
- Modify: `docs/decisions.md` (D6: interrupt hook on PreToolUse only; D7: plans and handoffs under the data dir; D8: prompt turns are not judged), `docs/design-spec.md` (§4.2, §4.4 state machine as built, §6 Esc during planning, §10 M3 done), `docs/flows.md` (section 7 interrupt diagram: PreToolUse only; section 5 lifecycle: `failed_verify`, `planned`), `README.md` (status, the full `queue:` command list)

- [ ] **Step 1: Edit the four files**

- [ ] **Step 2: Mermaid balance check** (same one-liner as the M2 plan Task 8)

- [ ] **Step 3: Commit**

```bash
git add docs README.md
git commit -m "docs: M3 decisions, spec and diagrams as built"
```

---

### Task 8: Live acceptance for M3

**Files:**
- Create: `docs/m3-acceptance.md`

**Interfaces:**
- Consumes: the tmux method; the driver in the scratchpad clears the input line before each send.

- [ ] **Step 1: Scenario T, idle markers.** At idle type `later: write i.txt containing iota`: expected the file is written now and the log shows `marker_stripped`. Then `maybe later we can add a README`: expected `parked p2` on screen, nothing runs. Then `stop`: expected `nothing is running`.
- [ ] **Step 2: Scenario U, Esc during planning.** Queue a task, and while Claude writes its plan press Esc (`tmux send-keys -t qsc Escape`), then type `what time is it`. Expected: the notice says the planning turn was interrupted; the task is `queued` again; after the new turn ends it is planned again.
- [ ] **Step 3: Scenario V, two sessions.** Open a second tmux session in `/tmp/qsc-demo-2` with the plugin; queue a task in each; run `/queue-switchclaude:queue list` in each. Expected: each lists only its own task.
- [ ] **Step 4: Scenario W, marketplace install.** In a fresh scratch folder without `--plugin-dir`: `/plugin marketplace add 0xEl1yahu/queue-switchclaude`, `/plugin install queue-switchclaude@queue-switchclaude`, then scenario A from M0. Expected: the plugin loads from the marketplace and queues the task. Uninstall afterwards.
- [ ] **Step 5: Record and commit**

```bash
git add docs/m3-acceptance.md
git commit -m "docs: M3 acceptance results"
```
