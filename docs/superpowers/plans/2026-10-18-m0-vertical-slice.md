# M0 Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code plugin that, in one session, queues a task typed mid-run, runs it after the current one finishes, and stops the turn when the user says "stop".

**Architecture:** Three Python entry points under `scripts/`, one per hook event (`UserPromptSubmit`, `Stop`, `PreToolUse`/`PostToolUse`), sharing a stdlib-only package `scripts/qsc/` for the store, rules, classifier wrapper and log. One JSON store per session under `$CLAUDE_PLUGIN_DATA`. Classification is rules first; what rules cannot decide goes to `claude -p --model haiku --json-schema`. A `shadow` user setting logs decisions without acting.

**Tech Stack:** Python 3.10+ stdlib only (`json`, `fcntl`, `tempfile`, `subprocess`, `re`), `pytest` for tests, Claude Code plugin layout (`.claude-plugin/plugin.json`, `hooks/hooks.json`), `claude` CLI for inference.

**Spec:** `docs/design-spec.md` (sections 3, 4, 10 M0, 11) and `docs/decisions.md` (D2, D2b, D5).

## Global Constraints

- Hook scripts run with whatever `python3` is on PATH: no third-party imports anywhere under `scripts/`. Tests may use `pytest`.
- Every hook script exits 0 with no output on any internal error, malformed stdin, or unknown event. A broken plugin must look like no plugin.
- Classifier call, verbatim flags: `claude -p --model haiku --setting-sources "" --strict-mcp-config --tools "" --output-format json --json-schema <schema> --system-prompt <prompt> <payload>`, timeout 15 s. Any failure means `steer` and a `null` classifier field in the log.
- A message is queued only when a rule matches, or the model answers `queue` with `confidence >= 0.9`.
- Store path: `$CLAUDE_PLUGIN_DATA/sessions/<session_id>.json`; fallback `~/.claude/queue-switchclaude/sessions/<session_id>.json` when the variable is unset. Log path: same base, `log/<session_id>.jsonl`.
- Task ids are `p1`, `p2`, ... per session, never reused within a session.
- `hooks/hooks.json` uses exec form (`command` + `args`) with `${CLAUDE_PLUGIN_ROOT}`; never shell form.
- Blocking a prompt uses `{"decision": "block", "reason": ...}`; the reason is the only thing the user sees, so it names the task id.

## Review Focus

1. A pasted block (150+ words or 12+ lines) typed mid-turn: must never be queued by a rule and never split; it goes to the model or passes as steer. Test in Task 3.
2. "stop" inside a longer sentence ("don't stop the dev server"): must not interrupt. Only a message that *is* a stop phrase interrupts. Test in Task 3.
3. Empty stdin, non-JSON stdin, or a payload for another event: every script exits 0 with empty stdout. Test in Task 2.
4. A corrupt or truncated store file: treated as a fresh store, the corruption logged, never a crash. Test in Task 1.
5. `claude` missing from PATH or hanging: the wrapper returns `None` within the timeout and intake passes the message through. Test in Task 4.

---

### Task 1: Store

**Files:**
- Create: `scripts/qsc/__init__.py` (empty)
- Create: `scripts/qsc/store.py`
- Create: `scripts/qsc/paths.py`
- Test: `tests/test_store.py`
- Create: `tests/conftest.py` (adds `scripts/` to `sys.path`; sets `CLAUDE_PLUGIN_DATA` to `tmp_path` via a fixture `data_dir`)

**Interfaces:**
- Produces: `paths.data_dir() -> Path` (reads `CLAUDE_PLUGIN_DATA`, else `~/.claude/queue-switchclaude`), `paths.store_path(session_id) -> Path`, `paths.log_path(session_id) -> Path`.
- Produces: `store.Task` dataclass: `id: str`, `text: str`, `status: str` (`queued | running | done | interrupted | dropped`), `depends_on: list[str]`, `prompt_id: str | None`, `created_at: str` (ISO 8601 UTC).
- Produces: `store.Store` dataclass: `session_id: str`, `state: str` (`idle | running`), `current_prompt_id: str | None`, `running_task: str | None`, `next_id: int` (starts 1), `interrupt: bool`, `last_dispatched: str | None`, `tasks: list[Task]`.
- Produces: `store.load(session_id) -> Store` (fresh `Store` when the file is missing or unparseable), `store.save(s: Store) -> None` (lock on `<path>.lock` via `fcntl.flock`, write to a sibling temp file, `os.replace`), `store.locked(session_id)` context manager yielding a `Store` and saving on exit, `Store.new_task(text, depends_on, prompt_id) -> Task` (assigns `p{next_id}`, increments, appends), `Store.get(task_id) -> Task | None`, `Store.ready() -> list[Task]` (status `queued` and every dependency `done`, in creation order).

- [ ] **Step 1: Write the failing tests**

```python
def test_load_missing_returns_fresh(data_dir):
    s = store.load("s1")
    assert s.state == "idle" and s.tasks == [] and s.next_id == 1

def test_save_then_load_roundtrip(data_dir):
    s = store.load("s1"); t = s.new_task("add login", [], "pid-1"); store.save(s)
    s2 = store.load("s1")
    assert s2.tasks[0].id == "p1" and s2.tasks[0].text == "add login" and s2.next_id == 2

def test_corrupt_file_is_fresh(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True); p.write_text("{not json")
    assert store.load("s1").tasks == []

def test_ready_requires_all_deps_done(data_dir):
    s = store.load("s1")
    a = s.new_task("a", [], None); b = s.new_task("b", ["p1"], None); c = s.new_task("c", ["p1", "p2"], None)
    assert [t.id for t in s.ready()] == ["p1"]
    a.status = "done"
    assert [t.id for t in s.ready()] == ["p2"]
    b.status = "done"
    assert [t.id for t in s.ready()] == ["p3"]

def test_save_is_atomic_no_partial_file(data_dir):
    s = store.load("s1"); store.save(s)
    assert not list(paths.store_path("s1").parent.glob("*.tmp*"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'qsc'`

- [ ] **Step 3: Implement `paths.py` and `store.py` with the interfaces above**

`load` catches `FileNotFoundError`, `json.JSONDecodeError`, `KeyError`, `TypeError` and returns a fresh `Store`. `save` holds the lock for the whole write. Serialise with `dataclasses.asdict`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc tests/conftest.py tests/test_store.py
git commit -m "feat(qsc): per-session JSON store with lock and atomic save"
```

---

### Task 2: Hook I/O and decision log

**Files:**
- Create: `scripts/qsc/hookio.py`
- Create: `scripts/qsc/log.py`
- Test: `tests/test_hookio.py`

**Interfaces:**
- Produces: `hookio.read_event(expected: str | tuple[str, ...]) -> dict | None`: parses stdin JSON once; returns `None` when stdin is empty, not JSON, not a dict, or `hook_event_name` is not `expected` (or not in the tuple).
- Produces: `hookio.emit(obj: dict | None) -> None`: writes JSON to stdout when `obj` is not `None`, else nothing; always returns (never raises).
- Produces: `hookio.shadow() -> bool`: `CLAUDE_PLUGIN_OPTION_SHADOW` in `{"1", "true", "yes"}` (case-insensitive).
- Produces: `log.record(session_id: str, event: str, **fields) -> None`: appends one JSON line `{"ts", "event", **fields}` to `paths.log_path(session_id)`, creating directories; swallows every exception.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hookio.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `hookio.py` and `log.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hookio.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/hookio.py scripts/qsc/log.py tests/test_hookio.py
git commit -m "feat(qsc): hook stdin/stdout helpers and JSONL decision log"
```

---

### Task 3: Rules

**Files:**
- Create: `scripts/qsc/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Produces: `rules.Verdict` dataclass: `label: str` (`steer | queue | interrupt | pass | undecided`), `rule: str | None` (name of the rule that fired), `text: str` (the task text with any marker stripped).
- Produces: `rules.decide(message: str) -> Verdict`.

Rule order, first match wins (names in `rule`):

| rule | fires when | label |
|---|---|---|
| `harness_traffic` | message starts with `<` or contains `Another Claude session sent a message` or `[Subagent hand-back]` | `pass` |
| `non_task` | stripped lowercase message in `{"ok", "okay", "yes", "y", "continue", "go", "."}` or starts with `/` | `pass` |
| `paste_guard` | 150+ words or 12+ lines | `undecided` (never queue by rule) |
| `interrupt` | whole message, lowercased and stripped of trailing punctuation, matches `^(stop|halt|abort)( (that|this|it|now|everything))*( now)?$` or equals `don't do that` / `do not do that` | `interrupt` |
| `queue_marker` | matches `^p\d+:\s*(.+)$`, `^later:\s*(.+)$`, or `^(once|when|after) (that|this|it|you)('s|'re|'ve| is| are| have)? (done|finished|in|through)[,:]?\s*(.+)$`, or `^after that[,:]?\s*(.+)$`, or `^then[,:]?\s*(.+)$` (case-insensitive) | `queue`, `text` = the captured remainder |
| (none) | | `undecided`, `text` = message |

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("msg,label,rule", [
    ("<system-reminder>x</system-reminder>", "pass", "harness_traffic"),
    ("ok", "pass", "non_task"), ("/compact", "pass", "non_task"), (".", "pass", "non_task"),
    ("stop", "interrupt", "interrupt"), ("Stop that now", "interrupt", "interrupt"), ("don't do that", "interrupt", "interrupt"),
    ("don't stop the dev server", "undecided", None),
    ("p2: add a /login endpoint", "queue", "queue_marker"),
    ("later: email verification", "queue", "queue_marker"),
    ("once that's done, write the alembic migration for it", "queue", "queue_marker"),
    ("when you're done, list the files", "queue", "queue_marker"),
    ("use bcrypt for the hashing", "undecided", None),
    ("add password reset as well", "undecided", None),
])
def test_rules(msg, label, rule):
    v = rules.decide(msg); assert (v.label, v.rule) == (label, rule)

def test_queue_marker_strips_prefix():
    assert rules.decide("p2: add a /login endpoint").text == "add a /login endpoint"
    assert rules.decide("once that's done, write the migration").text == "write the migration"

def test_paste_guard_never_queues():
    big = "\n".join(f"{i}) later: thing {i}" for i in range(13))
    assert rules.decide(big).label == "undecided"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_rules.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `rules.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_rules.py -v`
Expected: 16 passed (14 parametrised + 2)

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/rules.py tests/test_rules.py
git commit -m "feat(qsc): deterministic intake rules"
```

---

### Task 4: Classifier wrapper

**Files:**
- Create: `scripts/qsc/classify.py`
- Create: `prompts/intake_system.md`
- Test: `tests/test_classify.py`
- Create: `tests/fakes/claude` (executable shell script; echoes the JSON in `$FAKE_CLAUDE_OUTPUT`, or sleeps `$FAKE_CLAUDE_SLEEP` seconds)

**Interfaces:**
- Produces: `classify.SCHEMA: dict`, the JSON Schema: object with required `label` (enum `steer, queue, interrupt, unclear`), `confidence` (number 0..1), `depends_on` (array of string), `additionalProperties: false`.
- Produces: `classify.Decision` dataclass: `label: str`, `confidence: float`, `depends_on: list[str]`.
- Produces: `classify.payload(state: str, running_task: dict | None, queue: list[dict], recent: list[dict], message: str) -> dict` (the JSON the model sees; `queue` items are `{id, text, status, depends_on}`; `recent` is `[{role, text}]`).
- Produces: `classify.run(payload: dict, timeout: float = 15.0, claude: str = "claude") -> Decision | None`: builds the verbatim command from Global Constraints, `system-prompt` read from `prompts/intake_system.md` (path relative to `scripts/`), parses `structured_output` from the CLI's JSON, validates against `SCHEMA` by hand (label in enum, 0 <= confidence <= 1, depends_on list of str). Returns `None` on `FileNotFoundError`, `TimeoutExpired`, non-zero exit, parse or validation failure.

`prompts/intake_system.md` content: the class table from spec §3 reduced to `steer`, `queue`, `interrupt`, `unclear`; the state table; the rule "answer `queue` only for new scope the user wants done after the current work, with `depends_on` naming the running task id when the new work needs it"; and the rationales of golden cases g01, g04, g05, g07, g08, g12, g15, g18, g29, g30, g36, g37 as worked examples. Ends with "Answer with JSON only."

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_classify.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `tests/fakes/claude`, make it executable, implement `classify.py`, write `prompts/intake_system.md`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_classify.py -v`
Expected: 5 passed

- [ ] **Step 5: Live check against the real CLI on golden case g05**

Run:
```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from qsc import classify
p = classify.payload('running', {'id': 'p1', 'text': 'Add a User model with hashed passwords (SQLAlchemy)'}, [], [], 'once that is done, write the alembic migration for it')
print(classify.run(p))"
```
Expected: `Decision(label='queue', confidence=..., depends_on=['p1'])` with confidence at or above 0.9, in under 8 s

- [ ] **Step 6: Commit**

```bash
git add scripts/qsc/classify.py prompts/intake_system.md tests/test_classify.py tests/fakes/claude
git commit -m "feat(qsc): claude -p classifier wrapper with JSON schema and fail-open"
```

---

### Task 5: Transcript tail

**Files:**
- Create: `scripts/qsc/transcript.py`
- Test: `tests/test_transcript.py`

**Interfaces:**
- Produces: `transcript.recent(path: str | None, n: int = 4, max_bytes: int = 65536) -> list[dict]`: reads at most the last `max_bytes` of the JSONL file, parses complete lines, returns the last `n` entries of `type in {"user", "assistant"}` as `[{"role", "text"}]`, text being the concatenated `text` blocks (a string `content` is used as is), skipping entries whose text starts with `<`. Returns `[]` when `path` is `None`, missing, or unreadable.

- [ ] **Step 1: Write the failing tests**

```python
def test_recent_last_n_user_and_assistant(tmp_path):
    p = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Adding the User model now."}]}},
            {"type": "user", "message": {"content": "<system-reminder>x</system-reminder>"}},
            {"type": "progress", "x": 1}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert transcript.recent(str(p), n=2) == [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Adding the User model now."}]

def test_recent_missing_path():
    assert transcript.recent("/nope.jsonl") == [] and transcript.recent(None) == []

def test_recent_truncated_first_line_ignored(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("x" * 70000 + "\n" + json.dumps({"type": "user", "message": {"content": "tail"}}) + "\n")
    assert transcript.recent(str(p), n=1, max_bytes=100) == [{"role": "user", "text": "tail"}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_transcript.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `transcript.py`**

Seek to `max(0, size - max_bytes)`, drop the first partial line when the seek was not 0.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_transcript.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/transcript.py tests/test_transcript.py
git commit -m "feat(qsc): transcript tail reader"
```

---

### Task 6: Intake script

**Files:**
- Create: `scripts/intake.py`
- Create: `scripts/qsc/intake.py` (logic, importable)
- Test: `tests/test_intake.py`

**Interfaces:**
- Consumes: `store`, `rules`, `classify`, `transcript`, `hookio`, `log` from Tasks 1 to 5.
- Produces: `qsc.intake.handle(event: dict, classifier=classify.run, shadow: bool = False) -> dict | None`: the hook's JSON output or `None` for pass-through. `scripts/intake.py` is `hookio.read_event("UserPromptSubmit")` → `handle(...)` → `hookio.emit(...)` inside a bare `try/except: pass`.

`handle` algorithm:

1. `v = rules.decide(event["prompt"])`. If `v.label == "pass"`: return `None` without touching the store (log nothing).
2. `with store.locked(session_id) as s:`
3. `mid_turn = s.state == "running" and event.get("prompt_id") == s.current_prompt_id`.
4. If not `mid_turn` and `s.state == "running"`: the previous turn was interrupted by Esc (D5). Mark the running task `interrupted`; mark every `queued` task `dropped`; build `notice = "queue-switchclaude: the previous turn was interrupted, dropped queued tasks: p3 (text), p4 (text)"` (omit when nothing was queued).
5. If not `mid_turn`: `s.state = "running"`, `s.current_prompt_id = event.get("prompt_id")`, `t = s.new_task(prompt, [], prompt_id)`, `t.status = "running"`, `s.running_task = t.id`. Log `intake` with `decision="new_turn"`. Return `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": notice}}` if a notice exists, else `None`.
6. Mid-turn: if `v.label == "undecided"`: `d = classifier(classify.payload(s.state, running, queue, transcript.recent(event.get("transcript_path")), v.text))`; label is `d.label` when `d` is not `None` and `d.confidence >= 0.9` and `d.label in {"queue", "interrupt"}`, else `steer`. Otherwise label is `v.label`.
7. Log `intake` with `message`, `rule`, `classifier` (`None` or `asdict(d)`), `label`, `shadow`.
8. If `shadow`: return `None`.
9. `interrupt`: `s.interrupt = True`; return `{"decision": "block", "reason": "queue-switchclaude: stopping at the next tool call"}`.
10. `queue`: chain dependencies (spec §10 M0): `deps = [last queued task id]` if any task has status `queued`, else `[s.running_task]` if set, else `[]`; when the model gave a non-empty `depends_on`, use that instead; `t = s.new_task(v.text, deps, prompt_id)`; return `{"decision": "block", "reason": f"queue-switchclaude: queued {t.id} after {deps or 'nothing'}: {t.text}"}`.
11. `steer`: return `None`.

- [ ] **Step 1: Write the failing tests**

```python
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
    assert store.load("s1").tasks[-1].id == "p1"
    assert '"label": "queue"' in paths.log_path("s1").read_text()

def test_esc_detected_drops_queue_with_notice(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    out = intake.handle(ev("something else", prompt_id="pid-2"))
    assert "dropped queued tasks: p2" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.get("p1").status == "interrupted" and s.get("p2").status == "dropped" and s.running_task == "p3"

def test_pass_rule_never_touches_store(data_dir):
    assert intake.handle(ev("ok")) is None and not paths.store_path("s1").exists()

def test_script_swallows_bad_stdin():
    r = subprocess.run([sys.executable, "scripts/intake.py"], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_intake.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `scripts/qsc/intake.py` and `scripts/intake.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_intake.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/intake.py scripts/qsc/intake.py tests/test_intake.py
git commit -m "feat: intake hook — rules, classifier, queue, interrupt flag, Esc drop"
```

---

### Task 7: Interrupt and dispatch scripts

**Files:**
- Create: `scripts/interrupt.py`, `scripts/qsc/interrupt.py`
- Create: `scripts/dispatch.py`, `scripts/qsc/dispatch.py`
- Test: `tests/test_interrupt.py`, `tests/test_dispatch.py`

**Interfaces:**
- Consumes: `store`, `hookio`, `log`.
- Produces: `qsc.interrupt.handle(event: dict) -> dict | None`: if `store.interrupt` is set: clear it, mark the running task `interrupted`, `state = "idle"`, `running_task = None`, log `interrupt`, return `{"continue": False, "stopReason": "queue-switchclaude: stopped as requested"}`; else `None`. The script calls `hookio.read_event(("PreToolUse", "PostToolUse"))` once.
- Produces: `qsc.dispatch.handle(event: dict, shadow: bool = False) -> dict | None`:
  1. If `s.interrupt`: same reset as `interrupt.handle`, return `None`.
  2. Mark `s.running_task` `done` (if any). `s.state = "idle"`, `s.running_task = None`.
  3. `nxt = s.ready()`; if empty: log `dispatch` with `next=None`, return `None`.
  4. Spin guard: if `event.get("stop_hook_active")` and `s.last_dispatched == nxt[0].id`: return `None`.
  5. Log `dispatch` with `next=nxt[0].id`, `shadow`. If `shadow`: return `None`.
  6. `nxt[0].status = "running"`, `s.running_task = nxt[0].id`, `s.state = "running"`, `s.last_dispatched = nxt[0].id` (`current_prompt_id` unchanged: a Stop-hook continuation keeps the original id).
  7. Return `{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": f"queue-switchclaude: the user queued task {id} to run after the previous work. Do it now: {text}"}}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_interrupt_flag_stops_and_resets(data_dir):
    with store.locked("s1") as s:
        t = s.new_task("x", [], "pid"); t.status = "running"; s.running_task = "p1"; s.state = "running"; s.interrupt = True
    out = interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"})
    assert out == {"continue": False, "stopReason": "queue-switchclaude: stopped as requested"}
    s = store.load("s1"); assert s.interrupt is False and s.state == "idle" and s.get("p1").status == "interrupted"

def test_interrupt_no_flag_passes(data_dir):
    assert interrupt.handle({"hook_event_name": "PreToolUse", "session_id": "s1"}) is None

def test_dispatch_marks_done_and_hands_over_next(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
        s.new_task("b", ["p1"], "pid")
    out = dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False})
    assert "p2" in out["hookSpecificOutput"]["additionalContext"] and "b" in out["hookSpecificOutput"]["additionalContext"]
    s = store.load("s1"); assert s.get("p1").status == "done" and s.get("p2").status == "running" and s.running_task == "p2"

def test_dispatch_nothing_ready_goes_idle(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "running"; s.running_task = "p1"; s.state = "running"
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").state == "idle"

def test_dispatch_spin_guard(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.last_dispatched = "p1"
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": True}) is None

def test_dispatch_shadow_does_not_change_status(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid")
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}, shadow=True) is None
    assert store.load("s1").get("p1").status == "queued"

def test_dispatch_after_interrupt_does_not_dispatch(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.interrupt = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").interrupt is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_interrupt.py tests/test_dispatch.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the four files**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_interrupt.py tests/test_dispatch.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/interrupt.py scripts/dispatch.py scripts/qsc/interrupt.py scripts/qsc/dispatch.py tests/test_interrupt.py tests/test_dispatch.py
git commit -m "feat: interrupt hook (continue:false) and Stop-hook dispatch of the next ready task"
```

---

### Task 8: Plugin packaging

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `hooks/hooks.json`
- Modify: `README.md` (add an "Install for development" section)
- Test: `tests/test_packaging.py`

**Interfaces:**
- Produces: a loadable plugin. `plugin.json`: `name: "queue-switchclaude"`, `version: "0.1.0"`, `description`, `author: {name: "0xEl1yahu"}`, `userConfig: {"shadow": {"type": "boolean", "title": "Shadow mode", "description": "Log what the harness would do without acting", "default": false}}`.
- `hooks/hooks.json`, exactly:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/intake.py"], "timeout": 25, "statusMessage": "queue-switchclaude: sorting your message"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/dispatch.py"], "timeout": 30}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/interrupt.py"], "timeout": 5}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/interrupt.py"], "timeout": 5}]}]
  }
}
```

- [ ] **Step 1: Write the failing test**

```python
def test_hooks_json_uses_exec_form_and_existing_scripts():
    h = json.load(open("hooks/hooks.json"))["hooks"]
    for event in ["UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse"]:
        hook = h[event][0]["hooks"][0]
        assert hook["command"] == "python3" and hook["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/scripts/")
        assert Path(hook["args"][0].replace("${CLAUDE_PLUGIN_ROOT}/", "")).exists()

def test_plugin_manifest_declares_shadow():
    m = json.load(open(".claude-plugin/plugin.json"))
    assert m["name"] == "queue-switchclaude" and m["userConfig"]["shadow"]["type"] == "boolean"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_packaging.py -v`
Expected: FAIL with `FileNotFoundError`

- [ ] **Step 3: Write both JSON files and the README section**

README section "Install for development":

```bash
claude plugin validate /Users/elibelilty/Documents/GitHub/queue-switchclaude
claude --plugin-dir /Users/elibelilty/Documents/GitHub/queue-switchclaude
```

plus one line on `/config` → Shadow mode, and where the store and log live.

- [ ] **Step 4: Run the test and the validator**

Run: `pytest tests/test_packaging.py -v && claude plugin validate .`
Expected: 2 passed; `Validation passed` (warnings about missing `homepage` are acceptable)

- [ ] **Step 5: Commit**

```bash
git add .claude-plugin/plugin.json hooks/hooks.json tests/test_packaging.py README.md
git commit -m "feat: package as a Claude Code plugin with a shadow-mode setting"
```

---

### Task 9: Live acceptance in a real session

**Files:**
- Create: `docs/m0-acceptance.md` (the script below with the observed results filled in)

**Interfaces:**
- Consumes: the installed plugin from Task 8.

- [ ] **Step 1: Start a session with the plugin in a scratch project**

Run: `mkdir -p /tmp/qsc-demo && cd /tmp/qsc-demo && git init -q && claude --plugin-dir /Users/elibelilty/Documents/GitHub/queue-switchclaude`

- [ ] **Step 2: Scenario A, queue then dispatch**

Type: `write a python script hello.py that prints hello, then run it`
While it runs, type: `later: add a goodbye.py that prints goodbye`
Expected: the second message is blocked with a reason naming `p2`; after Claude finishes `hello.py`, the transcript shows `Stop hook feedback` and Claude creates `goodbye.py` without further input. Store file shows `p1 done`, `p2 done`.

- [ ] **Step 3: Scenario B, interrupt**

Type: `count from 1 to 50, running a separate bash echo for each number`
After a few echoes, type: `stop`
Expected: the message is blocked with the interrupt reason; the turn ends at the next tool call with `queue-switchclaude: stopped as requested`; the store shows `p1 interrupted`, `interrupt: false`.

- [ ] **Step 4: Scenario C, steer passes through**

Type: `write a function that adds two numbers`
While it runs, type: `name it add_two`
Expected: the message is not blocked; the log line for it shows `label: steer` (by classifier or `undecided` rule) and the function is named `add_two`.

- [ ] **Step 5: Scenario D, shadow mode**

Set Shadow mode on in `/config`, repeat Scenario A.
Expected: nothing is blocked, nothing is dispatched, the log shows `label: queue` and `dispatch next: p2` with `shadow: true`.

- [ ] **Step 6: Record results and commit**

Write what happened for A to D, including measured delay on the `later:` message and on the classified message in C, into `docs/m0-acceptance.md`.

```bash
git add docs/m0-acceptance.md
git commit -m "docs: M0 acceptance results"
```
