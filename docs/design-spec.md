# Queue harness: design spec (reconstructed)

> **Provenance.** The Claude Code session that produced the original design and the 47-case golden set was deleted on 2026-10-18 before anything was committed. The only file that survived is [`queue-harness-plan.md`](../queue-harness-plan.md) (the "side plan", written 2026-10-18 17:50). This spec is rebuilt from that file. Everything below is either stated there or marked **(inferred)**. The golden set itself (`golden_queue_intake.jsonl`) is lost except for the seven cases the plan cites by id.

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
| `interrupt` (flag) | Stop the current work now. | Block the message, set a flag, return `{"continue": false}` from the next Pre/PostToolUse hook. |
| `parked` (flag) | A maybe-later item, not a definite queue entry. | Stored but never dispatched automatically. |
| `unclear` | Classifier cannot decide. | Falls back to `steer`. |

Classification rules carried over from the two reference projects:

- **Two signals for interrupt.** Interrupt ≥ 0.9 *and* a separate "explicit stop" signal ≥ 0.9. Queue ≥ 0.9 also requires the stop signal < 0.5.
- **Skip non-tasks.** "ok", "yes", "continue", slash commands. A lone `.` accepts the suggested next step.
- **Paste guard.** 150+ words or 12+ lines counts as one ask, whatever bullets it contains.
- **Fake-split guard.** Each part must carry its own content words, and no two parts may be about the same thing.
- **Filter harness traffic.** System reminders and agent-completion notices arrive through the same hook and must never become tasks.
- **Classifier context.** The classifier sees the recent turn (last assistant text from the transcript tail), so "this" and "that" resolve.

## 4. Architecture

```
message ─▶ UserPromptSubmit: INTAKE (classify) ─▶ QUEUE / DAG
Claude stops ─▶ Stop: DISPATCH (check → handoff → plan next → approve) ─▶ next task
any tool call ─▶ Pre/PostToolUse: INTERRUPT check
```

Three hooks, one store.

### 4.1 Intake (`UserPromptSubmit`)

1. Receive the prompt. The hook fires immediately even for a message sent mid-run (verified on Claude Code 2.1.280).
2. Mid-turn vs new turn: compare `prompt_id`. A mid-turn message carries the running turn's id; a new turn gets a fresh one. Messages sent during a Stop-hook continuation keep the original id.
3. Drop harness traffic and non-tasks.
4. Read the transcript tail from the hook payload's `transcript_path` and take the last assistant text as context.
5. Call the classifier with a 2 s timeout. Any error or timeout **fails open**: the message passes through as `steer`, and the log records a null classifier field so silent failure is visible.
6. Act on the class: steer passes through; queue/amend/parked/split write to the store and block the message; interrupt sets the flag and blocks.

### 4.2 Interrupt (`PreToolUse` / `PostToolUse`)

If the interrupt flag is set, return `{"continue": false}`. The Stop hook does not fire after that. Limitation: a text-only turn has no tool boundary, so it cannot be hard-stopped.

### 4.3 Dispatch (`Stop`)

1. **Check** whether the task that was running is done (see §7).
2. **Handoff**: file Claude's closing message against the finished task, and write `handoffs/pN.md`.
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

with its own system prompt. Otherwise the model answers the message instead of classifying it, and can trigger hooks recursively. `claude -p` is too slow for inline intake (the Stop hook has ~5 s and a model call does not fit), so anything on the inline path uses a direct API call; anything slower runs detached.

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

- Golden set `golden_queue_intake.jsonl`: 47 labelled cases. **Lost with the session.** Cases the plan names:

  | id | Message | Expected |
  |---|---|---|
  | g09 | mixed steer + queue | `split` |
  | g12 | "add a logout endpoint" (while a *login* task is queued) | new `queue` task, must not fold into login |
  | g16 | "skip the tests" | `amend` as a cancel, not extra text |
  | g20 | "login endpoint" (short phrase) | `split` must still work below a 3-word minimum |
  | g27 | mixed steer + queue, with dependency rewiring | `split` then `amend` |
  | g46 | mixed steer + queue | `split` |

- Scoring: exact label match, an `also_acceptable` list per case, asymmetric dependency scoring (a missed hard dependency costs more than a spurious one).
- Gate: hard dependency fails = 0, and every interrupt case correct.
- Baseline: word-overlap matching (claude-task-manager's own functions) got amend right in 2 of 4, split in 1 of 5, and produced one false fold (g12). Amend and split therefore need a semantic classifier.

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

## 11. Reference projects

- **Jev_steer_or_queue**: router, ~300 lines. Source of the hook plumbing, `prompt_id` detection, interrupt path, shadow mode and `docs/test-results.md`.
- **claude-task-manager**: queue, ~1,200 lines, `pending_tasks.py`. Source of the transcript-tail reader, detached Haiku judge, harness-traffic filter and store-hygiene lessons.

## 12. What was lost and needs rebuilding

1. The full 47-case golden set (only the six cases above are known).
2. The original wording of the design, including any class definitions beyond §3.
3. The M0 log record schema, if it had been drafted.
