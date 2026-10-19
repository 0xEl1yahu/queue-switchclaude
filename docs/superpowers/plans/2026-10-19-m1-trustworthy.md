# M1 Trustworthy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the M0 harness trustworthy: every golden case scored by an eval runner, `amend`, `split` and `parked` handled, a `queue:` control channel and `/queue` command, and resubmitted held text deduplicated.

**Architecture:** Same three Python hook scripts and `qsc` package as M0. New modules: `qsc/match.py` (stdlib similarity for amend targets), `qsc/control.py` (the `queue:` command language), and `eval/run_golden.py` (the runner). The classifier contract becomes the golden set's full `expected` shape (label, interrupt flag, parked flag, target, note, parts). Intake grows four branches (dedupe, amend, split, control) and keeps fail-open everywhere.

**Tech Stack:** Python 3.10+ stdlib only under `scripts/` (adds `difflib`), `pytest`, `claude -p` for inference, Claude Code plugin `commands/`.

**Spec:** `docs/design-spec.md` sections 3, 8, 10 (M1), 11; `docs/decisions.md` D2b, D5; `docs/m0-acceptance.md` issues 1 to 3; golden set `eval/golden_queue_intake.jsonl`.

## Global Constraints

- Everything under `scripts/` is Python 3.10+ stdlib only. No `rapidfuzz`, no `model2vec`: amend matching uses `difflib` and token sets.
- Every hook script still exits 0 with empty stdout on any error, malformed stdin or foreign event. New branches fail open to `steer`.
- The classifier call keeps the M0 flags verbatim: `claude -p --model haiku --setting-sources "" --strict-mcp-config --tools "" --output-format json --json-schema <schema> --system-prompt <prompt> <payload>`, 15 s timeout.
- Confidence threshold stays 0.9 for every actionable label (`queue`, `amend`, `split`, `interrupt`, parked `queue`).
- Blocks keep the M0 shape from `intake._block`: `decision`, `reason`, `systemMessage`, `hookSpecificOutput.suppressOriginalPrompt`.
- Task statuses after M1: `queued`, `parked`, `running`, `paused`, `done`, `interrupted`, `dropped`, `cancelled`, `shadow_queued`, `shadow_dispatched`. `Store.ready()` still returns `queued` tasks with every dependency `done`, and nothing else.
- Store files written by M0 must load unchanged; unknown keys in a task or store object are ignored, not treated as corruption.
- The golden set is read-only. The eval runner never edits it. Model answers are cached under `eval/cache/` (gitignored) keyed by case id and prompt hash.
- Dedupe window: 10 minutes, compared on normalised text (lowercase, whitespace collapsed, marker stripped).
- Tests that spawn scripts or read the golden set define `ROOT = Path(__file__).resolve().parent.parent` at module level; tests in a new file that need `ev(...)` or the `dec(...)`/`part(...)` helpers copy them from `tests/test_intake.py` rather than importing across test modules.

## Review Focus

1. A message that is a near-duplicate but not identical ("later: add goodbye.py" then "later: add a goodbye.py") must not be deduped away; dedupe is exact after normalisation only. Test in Task 6.
2. An amend whose target is ambiguous (two queued tasks about tests) must not pick one by a small margin; it must fall through to the model, and if the model is also unsure, pass as `steer`. Test in Task 3 and Task 6.
3. A split whose parts are all `steer` is not a split: the message passes through untouched with no task created. Test in Task 6.
4. `queue: cancel p9` for an id that does not exist, or `queue: promote p2` for a task that is not parked, must answer with a clear message and change nothing. Test in Task 5.
5. The eval runner with `claude` missing must still produce a rules-only report and a non-zero gate exit, never a traceback. Test in Task 8.

---

### Task 1: Store v2

**Files:**
- Modify: `scripts/qsc/store.py`
- Test: `tests/test_store.py` (append)

**Interfaces:**
- Consumes: M0 `store.Task`, `store.Store`, `load`, `save`, `locked`.
- Produces: `TASK_STATUSES` extended with `parked`, `paused`, `cancelled`, `shadow_queued`, `shadow_dispatched`.
- Produces: `Task` gains `norm_text: str = ""` (set by `new_task` via `store.normalise(text)`), `notes: list[str] = field(default_factory=list)` (amend appendices), `parked: bool = False` is **not** a field; parked is the status.
- Produces: `store.normalise(text: str) -> str`: lowercase, curly quotes straightened, whitespace collapsed, trailing `.,;:!?` stripped.
- Produces: `Store.new_task(text, depends_on, prompt_id, status="queued") -> Task` (status parameter added; `norm_text` filled).
- Produces: `Store.find_recent(norm_text: str, within_seconds: int, now: datetime | None = None) -> Task | None`: newest task with equal `norm_text` whose `created_at` is within the window and whose status is not `dropped` or `cancelled`.
- Produces: `Store.cancel(task_id) -> Task | None` (status `cancelled` if it was `queued`, `parked` or `paused`; else `None`), `Store.promote(task_id) -> Task | None` (`parked` to `queued`; else `None`), `Store.candidates() -> list[Task]` (tasks with status in `queued`, `parked`, `paused`, `running`, in creation order).
- Produces: `_from_dict` ignores unknown keys on both `Task` and `Store` (filters to dataclass field names) so a store written by a newer version still loads.

- [ ] **Step 1: Write the failing tests**

```python
def test_new_task_fills_norm_text_and_status(data_dir):
    s = store.load("s1"); t = s.new_task("  Add a /Login endpoint!  ", [], None, status="parked")
    assert t.norm_text == "add a /login endpoint" and t.status == "parked"

def test_find_recent_window_and_statuses(data_dir):
    s = store.load("s1"); t = s.new_task("add login", [], None)
    assert s.find_recent("add login", 600).id == "p1"
    t.created_at = "2020-01-01T00:00:00Z"
    assert s.find_recent("add login", 600) is None
    u = s.new_task("add login", [], None); u.status = "cancelled"
    assert s.find_recent("add login", 600) is None

def test_cancel_and_promote_rules(data_dir):
    s = store.load("s1"); a = s.new_task("a", [], None, status="parked"); b = s.new_task("b", [], None); b.status = "done"
    assert s.promote("p1").status == "queued" and s.promote("p1") is None
    assert s.cancel("p1").status == "cancelled" and s.cancel("p2") is None and s.cancel("p9") is None

def test_unknown_keys_are_ignored_not_corrupt(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"state": "idle", "future_field": 1, "next_id": 2, "tasks": [{"id": "p1", "text": "x", "status": "queued", "depends_on": [], "prompt_id": None, "created_at": "2026-10-19T00:00:00Z", "future": True}]}))
    s = store.load("s1"); assert s.get("p1").text == "x" and "store_corrupt" not in (paths.log_path("s1").read_text() if paths.log_path("s1").exists() else "")

def test_m0_store_file_loads(data_dir):
    p = paths.store_path("s1"); p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"session_id": "s1", "state": "idle", "current_prompt_id": None, "running_task": None, "next_id": 3, "interrupt": False, "last_dispatched": "p2", "tasks": [{"id": "p1", "text": "a", "status": "done", "depends_on": [], "prompt_id": "x", "created_at": "2026-10-18T21:59:04Z"}]}))
    assert store.load("s1").get("p1").norm_text == "" and store.load("s1").next_id == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_store.py -v -k "norm_text or find_recent or cancel_and_promote or unknown_keys or m0_store"`
Expected: FAIL (TypeError on `status=` keyword, AttributeError on `find_recent`)

- [ ] **Step 3: Implement the changes in `store.py`**

- [ ] **Step 4: Run the whole store test file**

Run: `pytest tests/test_store.py -v`
Expected: all pass, M0 tests untouched

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/store.py tests/test_store.py
git commit -m "feat(store): statuses, normalised text, find_recent, cancel/promote, forward-compatible load"
```

---

### Task 2: Decision contract v2 and prompt v2

**Files:**
- Modify: `scripts/qsc/classify.py`
- Modify: `prompts/intake_system.md`
- Test: `tests/test_classify.py` (append)

**Interfaces:**
- Consumes: M0 `classify.run` (unchanged call shape), `classify.payload`.
- Produces: `classify.LABELS = ("steer", "queue", "amend", "split", "unclear")`. `interrupt` is no longer a label; it is a flag, matching the golden set.
- Produces: `classify.Part` dataclass: `label: str`, `text: str`, `confidence: float`, `interrupt: bool = False`, `parked: bool = False`, `depends_on: list[str]`, `target: str | None = None`, `note: str | None = None` (`note` in `approve | revise plan | cancel | pause | None`).
- Produces: `classify.Decision` dataclass: `label: str`, `confidence: float`, `interrupt: bool`, `parked: bool`, `depends_on: list[str]`, `target: str | None`, `note: str | None`, `parts: list[Part]` (non-empty only for `split`).
- Produces: `classify.SCHEMA` v2: the JSON Schema for `Decision` with `parts.items` being the `Part` schema, `additionalProperties: false` everywhere, every field required (null allowed for `target`, `note`).
- Produces: `classify.payload(state, running_task, queue, recent, message, candidates=None) -> dict`: adds `"candidates": [{id, text, score}]` when given (Task 3 fills it for amend).
- Produces: `classify._validate` accepts v2 and returns `None` on any extra key or wrong type, as in M0.
- `prompts/intake_system.md` v2: the full class table from spec §3 (steer, queue, amend, split, unclear) with `interrupt` and `parked` as flags and `note` values; the state table including `awaiting_approval`; the rules (two signals for interrupt, fake-split guard, "unresolvable referent is unclear"); and worked examples using the `rationale` field of golden cases g04, g07, g08, g09, g12, g14, g16, g18, g20, g23, g25, g27, g31, g32, g33, g34, g36, g37, g41, g42, g43, g45, g46, g47. Ends with "Answer with JSON only."

- [ ] **Step 1: Write the failing tests**

```python
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
```

Also update the M0 tests in `tests/test_classify.py` that build a v1 `structured_output` to the v2 shape (same expectations otherwise).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_classify.py -v`
Expected: FAIL on the v2 tests

- [ ] **Step 3: Implement `classify.py` v2 and rewrite `prompts/intake_system.md`**

- [ ] **Step 4: Run the tests, then the live check on g09 and g16**

Run: `pytest tests/test_classify.py -v`
Expected: all pass

Run (two real `claude -p` calls, about $0.001):
```bash
python3 -c "
import sys, json; sys.path.insert(0, 'scripts')
from qsc import classify
rows = {json.loads(l)['id']: json.loads(l) for l in open('eval/golden_queue_intake.jsonl')}
for cid in ('g09', 'g16'):
    r = rows[cid]
    q = [{'id': t['id'], 'text': t['intent'], 'status': t['status'], 'depends_on': t['depends_on']} for t in r['queue']]
    ct = r['current_task'] and {'id': r['current_task']['id'], 'text': r['current_task']['intent']}
    print(cid, classify.run(classify.payload(r['state'], ct, q, r.get('recent', []), r['message'])))"
```
Expected: g09 → `label='split'` with a steer part and a queue part depending on p1; g16 → `label='amend'`, `target='p3'`, `note='cancel'`. If either misses, adjust the prompt's worked examples before committing; do not weaken the tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/classify.py prompts/intake_system.md tests/test_classify.py
git commit -m "feat(classify): v2 decision contract with amend, split, parked and interrupt flag"
```

---

### Task 3: Amend target matching

**Files:**
- Create: `scripts/qsc/match.py`
- Test: `tests/test_match.py`

**Interfaces:**
- Consumes: `store.normalise`, `store.Task`.
- Produces: `match.score(message: str, task_text: str) -> float`: `max(difflib.SequenceMatcher(None, a, b).ratio(), jaccard(tokens(a), tokens(b)))` on normalised strings, tokens being words of 3+ letters minus a stopword set (`the a an to for of in on and or it this that with`).
- Produces: `match.rank(message: str, tasks: list[store.Task]) -> list[tuple[store.Task, float]]`: sorted descending by score.
- Produces: `match.pick(message: str, tasks: list[store.Task], accept: float = 0.6, margin: float = 0.2) -> store.Task | None`: the top task when its score is at least `accept` and exceeds the runner-up by at least `margin` (or there is no runner-up); else `None`.
- Produces: `match.candidates(message, tasks, n=3) -> list[dict]`: top `n` as `{id, text, score}` for the classifier payload.

- [ ] **Step 1: Write the failing tests**

```python
def tasks(*texts):
    s = store.Store("s"); return [s.new_task(t, [], None) for t in texts]

def test_pick_clear_subject_reference():
    ts = tasks("Add a Stripe webhook handler", "Refactor the billing service")
    assert match.pick("the webhook handler should verify Stripe signatures", ts).id == "p1"   # g23

def test_pick_by_subject_word_tests():
    ts = tasks("Add a /login endpoint that returns a JWT", "Write tests for /login")
    assert match.pick("for the tests, include a rate-limit case", ts).id == "p2"              # g14

def test_pick_refuses_small_margin():
    ts = tasks("Write unit tests for billing", "Write unit tests for login")
    assert match.pick("for the tests, add a rate-limit case", ts) is None                       # review focus 2

def test_pick_refuses_low_score():
    ts = tasks("Add a Stripe webhook handler")
    assert match.pick("bump fastapi to the latest version", ts) is None                         # g25 is not an amend

def test_candidates_shape():
    ts = tasks("a b c", "d e f")
    c = match.candidates("a b", ts)
    assert c[0]["id"] == "p1" and set(c[0]) == {"id", "text", "score"} and len(c) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_match.py -v`
Expected: FAIL with `ImportError: cannot import name 'match'`

- [ ] **Step 3: Implement `match.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_match.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/match.py tests/test_match.py
git commit -m "feat(match): stdlib similarity for amend targets with accept and margin thresholds"
```

---

### Task 4: Rules v2

**Files:**
- Modify: `scripts/qsc/rules.py`
- Test: `tests/test_rules.py` (append)

**Interfaces:**
- Consumes: M0 `rules.Verdict`, `rules.decide`.
- Produces: `Verdict` gains `parked: bool = False` and `control: str | None = None`.
- Produces: new rules, inserted into the table in this order and position:

| rule | position | fires when | result |
|---|---|---|---|
| `control` | after `harness_traffic`, before `non_task` | message matches `^(queue|/queue(-switchclaude:queue)?)\s*:?\s*(.*)$` (case-insensitive) | label `control`, `control` = the remainder (may be empty, meaning `list`) |
| `parked_marker` | after `interrupt`, before `queue_marker` | starts with `maybe later`, `someday`, `at some point`, `one day`, `if we have time`, optionally followed by `,` or `:` and text | label `queue`, `parked=True`, `text` = remainder; if the remainder is empty or only a pronoun (`this`, `that`, `it`), label `undecided` (g34) |
| `queue_marker` | unchanged | | |
| `numbered_split` | after `queue_marker`, before the final `undecided` | two or more items matching `(^|\s)(\d+[).]|[-*])\s+\S` on a message under the paste guard | label `undecided`, `rule="numbered_split"` (the model does the split; the rule only names it for the log) |

- `LABELS` gains `control`. `non_task` no longer treats messages starting with `/queue` as pass (the `control` rule runs first).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("msg,label,rule,extra", [
    ("queue: list", "control", "control", "list"),
    ("queue:", "control", "control", ""),
    ("/queue cancel p2", "control", "control", "cancel p2"),
    ("/queue-switchclaude:queue promote p3", "control", "control", "promote p3"),
    ("maybe later we can add social login", "queue", "parked_marker", "we can add social login"),
    ("someday: dark mode", "queue", "parked_marker", "dark mode"),
    ("maybe later we can add this", "undecided", "parked_marker", None),
    ("1) login endpoint 2) logout endpoint 3) tests for both", "undecided", "numbered_split", None),
    ("later: email verification", "queue", "queue_marker", "email verification"),
])
def test_rules_v2(msg, label, rule, extra):
    v = rules.decide(msg)
    assert (v.label, v.rule) == (label, rule)
    if label == "control": assert v.control == extra
    if rule == "parked_marker" and label == "queue": assert v.parked and v.text == extra

def test_slash_other_commands_still_pass():
    assert rules.decide("/compact").label == "pass"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_rules.py -v -k "v2 or slash_other"`
Expected: FAIL

- [ ] **Step 3: Implement the rules**

- [ ] **Step 4: Run the whole rules file**

Run: `pytest tests/test_rules.py -v`
Expected: all pass, including M0 cases

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/rules.py tests/test_rules.py
git commit -m "feat(rules): control prefix, parked markers, numbered-split detection"
```

---

### Task 5: Control commands

**Files:**
- Create: `scripts/qsc/control.py`
- Test: `tests/test_control.py`

**Interfaces:**
- Consumes: `store.Store` (`candidates`, `cancel`, `promote`, `get`, `tasks`), `interrupt.reset_after_interrupt`.
- Produces: `control.run(s: store.Store, command: str) -> str`: executes one command against an already-locked store and returns the text to show the user. Commands (first word, case-insensitive; empty means `list`):

| command | effect | reply |
|---|---|---|
| `list` | none | one line per task with status in `running, queued, parked, paused`, in order: `p2  queued   after p1   add a goodbye.py…` (text cut at 60 chars); `queue is empty` when none |
| `cancel pN` | `s.cancel(pN)` | `cancelled pN: <text>` or `pN is not queued, parked or paused` / `no task pN` |
| `promote pN` | `s.promote(pN)` | `promoted pN to the queue` or `pN is not parked` / `no task pN` |
| `park pN` | status `queued` → `parked` | `parked pN` or `pN is not queued` |
| `drop` | every `queued` and `parked` → `dropped` | `dropped N tasks: p2, p3` |
| `pause` / `resume` | `s.paused = True/False` (new `Store` field, default `False`; dispatch dispatches nothing while paused, Task 7) | `queue paused` / `queue resumed` |
| `help` or unknown | none | the command list in one line each |

- [ ] **Step 1: Write the failing tests**

```python
def seeded():
    s = store.Store("s1"); a = s.new_task("add login", [], None); a.status = "running"; s.running_task = "p1"
    s.new_task("write tests for login", ["p1"], None); s.new_task("social login", ["p1"], None, status="parked"); return s

def test_list_shows_live_tasks():
    out = control.run(seeded(), "list")
    assert "p1" in out and "p2  queued" in out.replace("   ", "  ") and "p3  parked" in out.replace("   ", "  ")

def test_empty_command_is_list():
    assert control.run(store.Store("s1"), "") == "queue is empty"

def test_cancel_promote_park_messages():
    s = seeded()
    assert control.run(s, "cancel p2").startswith("cancelled p2") and s.get("p2").status == "cancelled"
    assert control.run(s, "cancel p9") == "no task p9"
    assert control.run(s, "promote p3") == "promoted p3 to the queue" and control.run(s, "promote p3") == "p3 is not parked"
    assert control.run(s, "park p3") == "parked p3" and control.run(s, "cancel p1") == "p1 is not queued, parked or paused"

def test_drop_and_pause():
    s = seeded()
    assert control.run(s, "drop") == "dropped 2 tasks: p2, p3" and all(t.status == "dropped" for t in s.tasks[1:])
    assert control.run(s, "pause") == "queue paused" and s.paused and control.run(s, "resume") == "queue resumed" and not s.paused

def test_unknown_shows_help():
    assert "cancel pN" in control.run(store.Store("s1"), "frobnicate")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_control.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Add `paused: bool = False` to `Store` (store.py) and implement `control.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_control.py tests/test_store.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/control.py scripts/qsc/store.py tests/test_control.py
git commit -m "feat(control): queue list/cancel/promote/park/drop/pause/resume"
```

---

### Task 6: Intake v2

**Files:**
- Modify: `scripts/qsc/intake.py`
- Test: `tests/test_intake.py` (append; update the M0 tests that assert `Decision("queue", 0.95, ["p1"])` to build a v2 `Decision`)

**Interfaces:**
- Consumes: Tasks 1 to 5. `classify.Decision` v2, `match.pick`, `match.candidates`, `control.run`, `store.find_recent`, `rules.Verdict.parked/control`.
- Produces: `intake.handle(event, classifier=classify.run, shadow=False) -> dict | None`, same signature, new behaviour:

1. `v = rules.decide(prompt)`. `pass` → `None` (unchanged).
2. `control` → inside `store.locked`: `reply = control.run(s, v.control)`; return `_block(f"{PREFIX}: {reply}")`. Works in every state, never touches the classifier, never counted as a turn. In shadow mode it still acts (control is the user's explicit command).
3. Inside the lock, `mid_turn` as in M0. Before `_new_turn`, **dedupe** (issue 1, second half): `dup = s.find_recent(store.normalise(v.text), 600)`; if `dup` and `dup.status in ("queued", "running", "done", "parked", "paused", "shadow_queued", "shadow_dispatched")`: log `intake` with `decision="dedupe", task=dup.id` and return `_block(f"{PREFIX}: that is already {dup.id} ({dup.status}); send 'queue: list' to see the queue")`. Applies to new-turn and mid-turn prompts alike.
4. Mid-turn, rules-decided `queue` with `v.parked`: create the task with `status="parked"`, deps from `_chain_deps`, return `_block(f"{PREFIX}: parked {t.id}: {t.text} (send 'queue: promote {t.id}' to run it)")`. Shadow: status `shadow_queued`, log `parked=True`, return `None`.
5. Mid-turn, `undecided`: build the payload with `candidates=match.candidates(v.text, s.candidates())` and call the classifier. Act on `decision` only when `confidence >= 0.9`:
   - `steer` with `interrupt=True` → the M0 interrupt branch.
   - `queue` → M0 queue branch; `parked=True` → status `parked` as in step 4.
   - `amend` → target resolution order: `decision.target` if it names a task in `s.candidates()`; else `match.pick(v.text, s.candidates())`; else `steer`. Apply, in this order of precedence: `note == "cancel"` → `s.cancel(target)`, confirmation `cancelled {id}: {text}`; `note == "pause"` → `target.status = "paused"` (only meaningful when the target is the running task; dispatch then leaves it `paused`, Task 7), confirmation `paused {id}`; `decision.depends_on` differs from `target.depends_on` (including `[]` to clear it, g27) → replace, confirmation `{id} now depends on {ids or 'nothing'}`; otherwise append `v.text` to `target.notes`, confirmation `noted on {id}: {text}`. A standalone amend returns `_block(confirmation)`. Inside a split, the confirmation joins the others (next bullet).
   - `split` → fake-split guard first: drop parts whose `text` is empty after `store.normalise`, and merge parts whose normalised `text` is identical (keep the first). If one part remains, treat the decision as that part's label. If every remaining part is `steer`, treat as `steer` (review focus 3). Else for each `queue`/`amend` part apply the branch above without blocking; collect the confirmations; if any part is `steer` (or `steer` with `note="approve"`), return `None` with `hookSpecificOutput.additionalContext = f"{PREFIX}: handled the rest of this message: {confirmations}; act only on: {steer parts joined}"` (the prompt passes through, Claude is told which part is its); if no part is `steer`, return `_block(confirmations joined)`.
   - anything else → `steer` (`None`).
6. Logging: every mid-turn decision logs `label`, `rule`, `classifier` (full v2 dict or `None`), `target`, `parts` (labels only), `shadow`, `prompt_id`, as M0 plus the new fields.

- [ ] **Step 1: Write the failing tests**

```python
def dec(**kw):
    base = dict(label="steer", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None, parts=[])
    base.update(kw); return classify.Decision(**base)

def part(**kw):
    base = dict(label="steer", text="", confidence=0.95, interrupt=False, parked=False, depends_on=[], target=None, note=None)
    base.update(kw); return classify.Part(**base)

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

def test_low_confidence_amend_is_steer(data_dir):
    intake.handle(ev("x")); intake.handle(ev("later: y"))
    assert intake.handle(ev("tweak y"), classifier=lambda p: dec(label="amend", target="p2", confidence=0.5)) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_intake.py -v`
Expected: the new tests FAIL; M0 tests that construct `Decision("queue", 0.95, ["p1"])` FAIL on the constructor until updated

- [ ] **Step 3: Implement intake v2 and update the M0 test constructors**

- [ ] **Step 4: Run the whole suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/intake.py tests/test_intake.py
git commit -m "feat(intake): control channel, dedupe, parked, amend, split"
```

---

### Task 7: Dispatch v2 and the `/queue` command

**Files:**
- Modify: `scripts/qsc/dispatch.py`
- Create: `commands/queue.md`
- Create: `scripts/queue_cli.py`
- Modify: `scripts/qsc/paths.py` (add `pointer_path()`), `scripts/qsc/intake.py` (write the pointer)
- Test: `tests/test_dispatch.py` (append), `tests/test_queue_cli.py`, `tests/test_packaging.py` (append)

**Interfaces:**
- Produces: `dispatch.handle` honours `s.paused` (logs `dispatch` with `paused=True`, dispatches nothing) and does not mark a `paused` running task `done` (it stays `paused`, `s.running_task` cleared).
- Produces: `paths.pointer_path() -> Path` = `data_dir() / "last_session"`; intake writes the session id there on every event (best effort, swallowed errors) so a Bash command can find the store without the session id.
- Produces: `scripts/queue_cli.py [--session ID] <command words>`: resolves the session from `--session`, else `$QSC_SESSION`, else the pointer file; runs `control.run` under `store.locked`; prints the reply; exit 0 always (prints `no session found` when none).
- Produces: `commands/queue.md`:

```markdown
---
description: Show or edit the queue-switchclaude task queue
argument-hint: [list | cancel pN | promote pN | park pN | drop | pause | resume]
allowed-tools: Bash(python3 *)
---
Run this exact command and print its output verbatim, nothing else:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/queue_cli.py" $ARGUMENTS
```

The `queue:` prefix in a prompt is the fast path (Task 6, no model, no turn); `/queue-switchclaude:queue` is the discoverable path that costs one short turn.

- [ ] **Step 1: Write the failing tests**

```python
def test_dispatch_respects_pause(data_dir):
    with store.locked("s1") as s:
        s.new_task("b", [], "pid"); s.paused = True
    assert dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"}) is None
    assert store.load("s1").get("p1").status == "queued" and '"paused": true' in paths.log_path("s1").read_text()

def test_dispatch_keeps_paused_running_task(data_dir):
    with store.locked("s1") as s:
        a = s.new_task("a", [], "pid"); a.status = "paused"; s.running_task = "p1"; s.state = "running"
    dispatch.handle({"hook_event_name": "Stop", "session_id": "s1"})
    assert store.load("s1").get("p1").status == "paused"

def test_queue_cli_uses_pointer(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "queue_cli.py"), "list"], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert r.returncode == 0 and "p2" in r.stdout

def test_queue_cli_no_session(data_dir):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "queue_cli.py"), "list"], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert r.returncode == 0 and r.stdout.strip() == "no session found"

def test_command_file_present_and_wired():
    text = Path("commands/queue.md").read_text()
    assert "queue_cli.py" in text and "allowed-tools: Bash(python3 *)" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_dispatch.py tests/test_queue_cli.py tests/test_packaging.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the four files and the pointer write**

- [ ] **Step 4: Run the suite and the validator**

Run: `pytest -q && claude plugin validate .`
Expected: all pass; `Validation passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/dispatch.py scripts/qsc/paths.py scripts/qsc/intake.py scripts/queue_cli.py commands/queue.md tests/test_dispatch.py tests/test_queue_cli.py tests/test_packaging.py
git commit -m "feat: pause-aware dispatch, queue_cli and the /queue command"
```

---

### Task 8: Eval runner

**Files:**
- Create: `eval/run_golden.py`
- Create: `eval/scoring.py`
- Modify: `.gitignore` (add `eval/cache/`, `eval/reports/`)
- Test: `tests/test_scoring.py`, `tests/test_run_golden.py`

**Interfaces:**
- Consumes: golden rows; `rules.decide`; `classify.payload`/`classify.run`; `match.candidates`.
- Produces: `scoring.outcome(expected: dict, got: dict) -> tuple[float, str]`: `got` is the harness's final decision for the case as a dict in the golden `expected` shape (`label`, plus `id`/`depends_on`/`parked`, `target`/`note`, `parts`, `interrupt`). Returns `(1.0, "ok")` on exact match of the relevant keys, ignoring `confidence`; `(-1.0, "missed_hard_dependency")` when `got.label == "queue"` and `got.depends_on` lacks an id that `expected.depends_on` has; `(0.0, reason)` otherwise, `reason` in `label`, `dependency_extra`, `target`, `note`, `parts`, `interrupt`, `parked`. Split cases compare part by part in order.
- Produces: `scoring.score_case(row: dict, got: dict) -> tuple[float, str]`: best outcome across `row["expected"]` and each `row["also_acceptable"]`.
- Produces: `run_golden.decide(row: dict, classifier) -> dict`: builds the harness decision for one case exactly as intake would (rules first; model only on `undecided`; threshold 0.9; `interrupt` from the rule or the flag; `parked` from the rule or flag; amend target via model-or-match using `row["queue"]` intents as task texts) but **without** a store, returning the golden-shaped dict. The task id for a new `queue` is `p{len(queue)+1}` (+1 per split part), matching the golden set's numbering.
- Produces: CLI `python3 eval/run_golden.py [--rules-only] [--cases g01,g05] [--no-cache] [--report eval/reports/<ts>.md]`: prints a table (id, context, difficulty, expected label, got label, score, reason), totals by label and difficulty, the count of `missed_hard_dependency`, the interrupt-case correctness, the share of cases decided by rules alone, and exits 0 when the gate passes (zero `-1.0`, every case with `expected.interrupt` scores 1.0) else 1. `--rules-only` never calls `claude`. The model cache is `eval/cache/<id>-<sha1 of prompt+payload>.json`.

- [ ] **Step 1: Write the failing tests**

```python
G = {json.loads(l)["id"]: json.loads(l) for l in open(ROOT / "eval" / "golden_queue_intake.jsonl")}

def test_scoring_exact_and_also_acceptable():
    assert scoring.score_case(G["g04"], {"label": "steer"}) == (1.0, "ok")
    assert scoring.score_case(G["g04"], {"label": "queue", "id": "p2", "depends_on": ["p1"]}) == (1.0, "ok")

def test_scoring_missed_hard_dependency_is_negative():
    assert scoring.score_case(G["g05"], {"label": "queue", "id": "p2", "depends_on": []}) == (-1.0, "missed_hard_dependency")

def test_scoring_extra_dependency_is_zero_not_negative():
    assert scoring.score_case(G["g08"], {"label": "queue", "id": "p2", "depends_on": ["p1"]})[0] == 0.0

def test_scoring_split_parts_in_order():
    got = {"label": "split", "parts": [{"label": "steer"}, {"label": "queue", "id": "p2", "depends_on": ["p1"]}]}
    assert scoring.score_case(G["g09"], got) == (1.0, "ok")

def test_scoring_interrupt_flag():
    assert scoring.score_case(G["g30"], {"label": "steer", "interrupt": True}) == (1.0, "ok")
    assert scoring.score_case(G["g30"], {"label": "steer"})[0] == 0.0

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_scoring.py tests/test_run_golden.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `scoring.py`, `run_golden.py`, the `.gitignore` lines**

`eval/` is not under `scripts/`, so it may `sys.path.insert` the `scripts` dir and import `qsc`; it stays stdlib-only anyway.

- [ ] **Step 4: Run the tests, then the rules-only eval**

Run: `pytest tests/test_scoring.py tests/test_run_golden.py -v && python3 eval/run_golden.py --rules-only`
Expected: tests pass; the rules-only report prints 47 rows and the share of cases the rules decide (record the number in the commit message).

- [ ] **Step 5: Run the full eval once with the model**

Run: `python3 eval/run_golden.py --report eval/reports/m1-first.md`
Expected: 47 model-classified rows at about 4 s each (3 to 4 minutes, roughly $0.02). Read the report. The gate may fail on this first run; that is the point of M1. Record the pass count, the `missed_hard_dependency` count and the interrupt result in `docs/m1-eval.md` (create it: date, command, totals table, the failing case ids with one line each on why).

- [ ] **Step 6: Commit**

```bash
git add eval/run_golden.py eval/scoring.py .gitignore tests/test_scoring.py tests/test_run_golden.py docs/m1-eval.md
git commit -m "feat(eval): golden-set runner with asymmetric dependency scoring and gate"
```

---

### Task 9: Close the gate

**Files:**
- Modify: `prompts/intake_system.md`, possibly `scripts/qsc/rules.py`, `scripts/qsc/match.py`
- Modify: `docs/m1-eval.md`
- Test: existing suites; the eval runner

**Interfaces:**
- Consumes: the failing cases from Task 8's report.

- [ ] **Step 1: For each failing case, classify the miss**

Rule miss (a rule fired wrongly or should have fired) → fix in `rules.py` with a regression test in `tests/test_rules.py`. Target miss → `match.py` thresholds or the candidates list, with a test in `tests/test_match.py`. Model miss → the worked examples or wording in `prompts/intake_system.md`. Never change `expected` in the golden set; if a case is genuinely wrong, add it to `docs/m1-eval.md` under "Disputed cases" and leave it failing.

- [ ] **Step 2: Re-run the eval after each change**

Run: `python3 eval/run_golden.py --no-cache --cases <ids that failed>` then the full run: `python3 eval/run_golden.py --report eval/reports/m1-gate.md`
Expected: exit 0: zero `missed_hard_dependency`, every interrupt case correct. Stop iterating when the gate passes or after four full runs; report the remainder in `docs/m1-eval.md`.

- [ ] **Step 3: Run the whole suite and commit**

Run: `pytest -q`
Expected: all pass

```bash
git add prompts/intake_system.md scripts/qsc tests docs/m1-eval.md
git commit -m "feat(eval): close the M1 gate — zero missed hard dependencies, all interrupts correct"
```

---

### Task 10: Shadow comparison

**Files:**
- Modify: `scripts/qsc/intake.py` (shadow compare), `.claude-plugin/plugin.json` (new option)
- Create: `eval/shadow_report.py`
- Test: `tests/test_intake.py` (append), `tests/test_shadow_report.py`

**Interfaces:**
- Produces: `userConfig.shadow_compare` (boolean, default `false`, title "Shadow: also ask the model"); `hookio.shadow_compare() -> bool` reads `CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE`.
- Produces: in shadow mode with `shadow_compare` on, intake calls the classifier even when a rule decided, and logs both under `rule_label` and `classifier`; the acted label stays the rule's.
- Produces: `eval/shadow_report.py [--since 7d]`: reads every `log/*.jsonl` under the data dir, prints counts of rule-decided vs model-decided messages, the rule/model disagreement rate when both are present, mean classifier latency where `latency_ms` is logged (add `latency_ms` to the intake log line in this task: wall-clock of the `claude -p` call), and the ten most recent disagreements with their texts.

- [ ] **Step 1: Write the failing tests**

```python
def test_shadow_compare_logs_both(data_dir, monkeypatch):
    intake.handle(ev("add a user model"))
    calls = []
    def clf(p): calls.append(p); return dec(label="steer")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SHADOW_COMPARE", "true")
    intake.handle(ev("later: email verification"), classifier=clf, shadow=True)
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert calls and row["rule_label"] == "queue" and row["classifier"]["label"] == "steer" and row["label"] == "queue"

def test_shadow_report_counts(data_dir):
    log.record("s1", "intake", label="queue", rule="queue_marker", rule_label="queue", classifier={"label": "steer"}, latency_ms=3400)
    log.record("s1", "intake", label="steer", rule=None, classifier={"label": "steer"}, latency_ms=3000)
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "shadow_report.py")], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert "disagreements: 1" in r.stdout and "mean classifier latency: 3200 ms" in r.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_intake.py -k shadow_compare tests/test_shadow_report.py -v`
Expected: FAIL

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the suite and the validator**

Run: `pytest -q && claude plugin validate .`
Expected: all pass; `Validation passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/qsc/intake.py scripts/qsc/hookio.py .claude-plugin/plugin.json eval/shadow_report.py tests
git commit -m "feat: shadow compare mode and shadow report"
```

---

### Task 11: Live acceptance for M1

**Files:**
- Create: `docs/m1-acceptance.md`

**Interfaces:**
- Consumes: the installed plugin, the tmux method from `docs/m0-acceptance.md`.

- [ ] **Step 1: Start the session**

Run: `rm -f /tmp/qsc-demo/*.py && cd /tmp/qsc-demo && tmux new-session -d -s qsc -x 150 -y 45 "claude --plugin-dir /Users/elibelilty/Documents/GitHub/queue-switchclaude --permission-mode auto"`

- [ ] **Step 2: Scenario E, dedupe**

Type a task, then mid-run `later: add a goodbye.py that prints goodbye`. When the turn ends and the held text sits in the input box, press Enter.
Expected: a `systemMessage` saying it is already `p2`, no new task, `next_id` unchanged.

- [ ] **Step 3: Scenario F, control channel**

Mid-run, type `later: a`, `later: b`, then `queue: list`, then `queue: cancel p3`, then `queue: list`.
Expected: each control message is blocked instantly (under 150 ms in the log) with the listing or confirmation on screen; `p3` cancelled; no model calls (`classifier: null` is absent because rules decided; the log shows `rule: control`).

- [ ] **Step 4: Scenario G, parked and promote**

Mid-run, type `maybe later we can add a README`. After the turn ends, confirm nothing dispatches; type `queue: promote p2`; confirm the next Stop dispatches it.

- [ ] **Step 5: Scenario H, amend by subject**

Mid-run, type `later: add a goodbye.py that prints goodbye`, then `the goodbye script should also print the date`.
Expected: `noted on p2` on screen; after dispatch, `goodbye.py` prints the date too (the handoff text includes the notes; add `notes` to `dispatch._handoff` if it does not).

- [ ] **Step 6: Scenario I, split**

Mid-run, type `name the function add_two, and after that add a subtract function`.
Expected: the message reaches Claude with the additional context naming only the first part; `p2` queued for the second; after dispatch, both functions exist.

- [ ] **Step 7: Scenario J, `/queue` command**

Type `/queue-switchclaude:queue list`.
Expected: Claude runs the CLI and prints the queue in one short turn.

- [ ] **Step 8: Record and commit**

Write results, delays and any deviation into `docs/m1-acceptance.md`.

```bash
git add docs/m1-acceptance.md scripts/qsc/dispatch.py
git commit -m "docs: M1 acceptance results"
```
