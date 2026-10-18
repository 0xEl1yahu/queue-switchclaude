# Queue harness: design spec (reconstructed)

> **Provenance.** The Claude Code session that produced the original design and the 47-case golden set was deleted on 2026-10-18 before anything was committed. The only file that survived is [`queue-harness-plan.md`](../queue-harness-plan.md) (the "side plan", written 2026-10-18 17:50). This spec is rebuilt from that file. Everything below is either stated there, taken from the golden set, or marked **(inferred)**. The golden set was recovered separately on 2026-10-18 and lives at [`eval/golden_queue_intake.jsonl`](../eval/golden_queue_intake.jsonl) (47 cases). Diagrams of the flows are in [flows.md](flows.md).

## 1. Problem

Claude Code treats every message typed mid-run as part of the running turn. A "when you're done, list the files" message gets injected into the current turn and runs in parallel with it. There is no native way to say "do this *after*", "change the thing you haven't started yet", or "stop".

## 2. Goal

Messages sent to Claude Code are sorted automatically into **steer / queue / amend / split**, plus two flags, **interrupt** and **parked**. Queued work runs in dependency order. Each task is planned just in time, from the previous task's handoff note.

## 3. Message classes

| Class | Meaning | Default behaviour |
|---|---|---|
| `steer` | Applies to the turn that is running now. | Pass through natively, untouched. This is also the fallback for anything unresolvable. |
| `queue` | A new task to run later. | Block the message, store it as a task, infer its dependencies, run it when its dependencies are done. |
| `amend` | Changes a task already in the queue: extra text, a cancel, or rewiring its dependencies. | Find the target task semantically and update it. "Skip the tests" is a cancel, not extra text (g16). |
| `split` | One message carrying more than one intent, e.g. steer + queue, or approve + queue. | Split into parts, classify each part. |
| `interrupt` (flag on `steer`) | Stop the current work now. | Block the message, set a flag, return `{"continue": false}` from the next Pre/PostToolUse hook. Cases g29, g30. |
| `parked` (flag on `queue`) | A maybe-later item ("maybe later we can add social login", g33), not a definite queue entry. | Stored with its dependencies but never dispatched until the user promotes it. "later: email verification" (g35) is a definite queue item, not parked. |
| `unclear` | Classifier cannot decide, or a referent cannot be resolved ("maybe later we can add this", g34). | Falls back to `steer`: Claude has the context to ask; the harness must never invent a task with no content. |

`steer` also carries a `note` in some states: `approve` or `revise plan` while a plan awaits approval (g44, g45, g47), `pause p1` for a reprioritisation (g27). `amend` carries `note: cancel` when it removes a task (g16, g43).

**Harness state changes what words mean.** The classifier always sees the state:

| State | Meaning | Effect on classification |
|---|---|---|
| `running` | A task is executing. | Corrections, constraints, answers to Claude's questions and status questions are `steer`. New scope is `queue`. |
| `idle` | Nothing running, queue empty or all done. | Anything actionable is `queue` and runs at once if its dependencies are done, even if phrased as a correction ("the user model's missing a unique constraint", g18). Questions stay `steer` (g19). |
| `awaiting_approval` | A plan for the next task is waiting. | "lgtm" is `steer: approve`; "looks good but do X first" is `steer: revise plan`, never approve (g45); "go, and once it's in add logout" is `split` (g46). |

Classification rules carried over from the two reference projects:

- **Two signals for interrupt.** Interrupt ≥ 0.9 *and* a separate "explicit stop" signal ≥ 0.9. Queue ≥ 0.9 also requires the stop signal < 0.5.
- **Skip non-tasks.** "ok", "yes", "continue", slash commands. A lone `.` accepts the suggested next step.
- **Paste guard.** 150+ words or 12+ lines counts as one ask, whatever bullets it contains.
- **Fake-split guard.** Each part must carry its own content words, and no two parts may be about the same thing.
- **Filter harness traffic.** System reminders and agent-completion notices arrive through the same hook and must never become tasks.
- **Classifier context.** The classifier sees the recent turn (last assistant and user text from the transcript tail), so "this", "that", "same" and "nvm" resolve. The same words flip class with context: "this is necessary" answers a question in g31 but is a bare acknowledgement in g32; "nvm" undoes an applied steer in g42 but cancels a queued task in g43.

## 4. Architecture

```
message ─▶ UserPromptSubmit: INTAKE (classify) ─▶ QUEUE / DAG
Claude stops ─▶ Stop: DISPATCH (check → handoff → plan next → approve) ─▶ next task
any tool call ─▶ Pre/PostToolUse: INTERRUPT check
```

Three hooks, one store. See [flows.md](flows.md) for the diagrams.

### 4.1 Intake (`UserPromptSubmit`)

1. Receive the prompt. The hook fires immediately even for a message sent mid-run (verified on Claude Code 2.1.280).
2. Mid-turn vs new turn: compare `prompt_id`. A mid-turn message carries the running turn's id; a new turn gets a fresh one. Messages sent during a Stop-hook continuation keep the original id.
3. Drop harness traffic and non-tasks.
4. Read the transcript tail from the hook payload's `transcript_path` for the recent turns (the `recent` field in the golden set). The docs warn the transcript "may lag the in-memory conversation", so treat it as best-effort context.
5. Call the classifier with a 2 s timeout. Any error or timeout **fails open**: the message passes through as `steer`, and the log records a null classifier field so silent failure is visible.
6. Act on the class: steer passes through; queue/amend/parked/split write to the store and block the message; interrupt sets the flag and blocks.

### 4.2 Interrupt (`PreToolUse` / `PostToolUse`)

If the interrupt flag is set, return `{"continue": false}`. The Stop hook does not fire after that. Limitation: a text-only turn has no tool boundary, so it cannot be hard-stopped.

### 4.3 Dispatch (`Stop`)

1. **Check** whether the task that was running is done (see §7). The closing message comes from the hook's `last_assistant_message` field, which the docs recommend over reading the transcript.
2. **Handoff**: file that closing message against the finished task, and write `handoffs/pN.md`.
3. **Plan next**: pick the next ready task (all `depends_on` satisfied), write `plans/pN.md` from the handoff note just in time. If a child's plan was written earlier and the parent's outcome changed, re-plan it.
4. **Approve**: gate on the plan (see decision D3).
5. Hand the task to Claude via `hookSpecificOutput.additionalContext`. `decision: "block"` also works but shows as "Stop hook error" in the UI, so it is not used.
6. **Spin guard**: if the queue looks identical across two consecutive stops, do not push again.

### 4.4 Store

- One queue per session, per-session file locks, atomic writes via tmp + `os.replace`.
- Ids scoped per queue; never look up an id across projects (that bug once closed another project's task).
- Task states **(inferred)**: `queued`, `parked`, `ready`, `planned`, `running`, `done`, `cancelled`.
- Fields **(inferred)**: id, text, class, `depends_on[]`, parked flag, origin `prompt_id`, context snippet, plan path, handoff path, closing message.
- Cycle check on every dependency write.

### 4.5 Model calls from hooks

Any model call made from a hook runs isolated:

```
claude -p --setting-sources "" --strict-mcp-config --exclude-dynamic-system-prompt-sections
```

with its own system prompt (or the Python Agent SDK with `setting_sources=[]`, see [libraries.md](libraries.md)). Otherwise the model answers the message instead of classifying it, and can trigger hooks recursively. `claude -p` is too slow for inline intake, so anything on the inline path uses a direct API call; anything slower runs detached. Note: the official hooks reference gives command hooks a 600 s default timeout (30 s on `UserPromptSubmit`), so the "Stop hook has 5 s" figure from claude-task-manager is not a current constraint. The 2 s budget is about the user waiting, not the hook dying.

## 5. Latency budget

Inline classification must answer in ~1–2 s. Reference numbers: 438–660 ms typical, occasionally ~1.5 s, 2 s timeout that fails open.

## 6. Policies

- **Shadow mode first.** Log decisions without acting on them, then switch to active.
- **Esc fires no hook.** Policy: pause the queue on Esc, do not drop it.
- **Automatic continuation** can annoy. Continuation into the next task is configurable per task or per queue; the approval gate is the default protection.
- **Keep the answer, not just the ask.** The closing message is filed against the finished task and seeds the handoff note.

## 7. Done detection (open, D4)

Options: a verify command per task; a detached Haiku "judge" that reads Claude's closing message plus the user's next message, marks tasks done conservatively ("ok, merge" counts as confirmation) and detects replies to a question; or both (run the command if present, judge otherwise).

## 8. Evaluation

The golden set [`eval/golden_queue_intake.jsonl`](../eval/golden_queue_intake.jsonl) holds 47 labelled cases. One JSON object per line:

| Field | Meaning |
|---|---|
| `id` | `g01`–`g47`. |
| `context` | Scenario family A–E. A: User model running, empty queue. B: login endpoint running, tests queued. C: idle after p1 done. D: billing refactor running, webhook queued. E: login plan awaiting approval. |
| `state` | `running`, `idle` or `awaiting_approval`. |
| `current_task` | `{id, intent}` (+ `plan` path in E), or `null` when idle. |
| `queue` | Existing tasks: `{id, intent, status, depends_on}`. |
| `recent` | Optional last turns `[{role, text}]`, present in 11 cases. This is what resolves "this", "that", "nvm". |
| `message` | The user's message. |
| `expected` | `{label}` plus, by label: `id` + `depends_on` (+ `parked`) for queue; `target` (+ `note`) for amend; `parts[]` for split; `interrupt: true` or `note` for steer. |
| `also_acceptable` | Alternative expected objects that score as correct (6 cases). |
| `difficulty` | easy 12, medium 18, hard 17. |
| `rationale` | Why the label is what it is. Doubles as classifier-prompt material. |

Label distribution: steer 24, queue 14, split 5, amend 4. Interrupt cases: g29, g30. Parked: g33.

Scoring:

- Exact label match against `expected`, or against any entry in `also_acceptable`.
- Dependency scoring is asymmetric: a missed hard dependency (the task would run before something it needs) costs more than a spurious one (the task waits longer than necessary). g25 shows the tolerated direction: an independent dependency bump may also wait for the running refactor.
- Split cases score per part, in order.
- Gate for M1: hard dependency fails = 0, and every interrupt case correct.

Baseline from the plan: word-overlap matching got amend right in 2 of 4 and split in 1 of 5, and folded g12 into the wrong task. Amend and split need a semantic classifier.

## 9. Decisions still open

| | Decision | Options |
|---|---|---|
| D1 | Base | (a) fork the Jev router skeleton (hook plumbing, `prompt_id`, shadow mode, interrupt path); (b) build fresh; (c) extend claude-task-manager (4× larger, different philosophy) |
| D2 | Classifier backend, ~1–2 s | Anthropic API with Haiku; local model via Ollama on the lab PC; TypeSafe Jev (fastest to try, third-party, sends message text off-machine) |
| D3 | Dispatch default | run the next ready task automatically, or approve each plan |
| D4 | Done detection | verify command, model judge, or both |
| D5 | Esc | pause the queue (preferred), or drop with a notice |

## 10. Milestones

- **M0 Shadow logger.** Hooks only, no model. Log every prompt with `prompt_id`, mid-turn flag, transcript tail and harness-traffic filter. Run for days, harvest real messages into the golden set. Hand-written spec: the log record schema.
- **M1 Classifier + eval.** Runner over the golden set and one backend from D2. Hand-written spec: the classifier's input/output contract.
- **M2 Queue + dispatch.** Task store, Stop-hook handover, interrupt, spin guard, Esc policy. Hand-written spec: the state machine.
- **M3 Plan flow.** `plans/pN.md`, approval gate, `handoffs/pN.md`, re-planning stale children.

## 11. Libraries

Per problem area, with code sketches: [libraries.md](libraries.md).

## 12. Reference projects

- **Jev_steer_or_queue**: router, ~300 lines. Source of the hook plumbing, `prompt_id` detection, interrupt path, shadow mode and `docs/test-results.md`.
- **claude-task-manager**: queue, ~1,200 lines, `pending_tasks.py`. Source of the transcript-tail reader, detached Haiku judge, harness-traffic filter and store-hygiene lessons.

## 13. What was lost and needs rebuilding

1. The original wording of the design beyond what the plan and the golden set pin down.
2. The M0 log record schema, if it had been drafted.
