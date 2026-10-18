# Queue harness: side plan

Goal: messages sent to Claude Code are sorted automatically into **steer / queue / amend / split** (plus `interrupt` and `parked` flags). Queued work runs in dependency order, and each task is planned just in time from the previous task's handoff.

Status: design + golden set (47 cases, `golden_queue_intake.jsonl`). No code yet.
Last updated: 2026-10-18, after reading `Jev_steer_or_queue` (router, ~300 lines) and `claude-task-manager` (queue, ~1,200 lines).

---

## 1. Architecture (unchanged)

```
message ─▶ UserPromptSubmit: INTAKE (classify) ─▶ QUEUE / DAG
Claude stops ─▶ Stop: DISPATCH (check → handoff → plan next → approve) ─▶ next task
any tool call ─▶ Pre/PostToolUse: INTERRUPT check
```

## 2. Open questions now answered

| Question | Answer | Evidence |
|---|---|---|
| Does `UserPromptSubmit` fire for a message sent mid-run? | Yes, immediately (tested on Claude Code 2.1.280). | Jev `docs/test-results.md` §1 |
| How do we tell mid-turn from a new turn? | `prompt_id`: a mid-turn message carries the running turn's id, a new turn gets a fresh one. Messages sent during a Stop-hook continuation keep the original id. | Jev `jev_router.py:210-216`, test-results §1 |
| Is native behaviour really a problem? | Yes. A "when you're done, list the files" message was injected into the current turn and ran in parallel with it. | test-results §1, row 2 |
| How do we implement `interrupt`? | Block the message, set a flag, and return `{"continue": false}` from the next Pre/PostToolUse hook. The Stop hook does not fire after that. | `jev_router.py:239-252`, test-results §1 |
| How does the dispatcher hand over the next task? | Return `hookSpecificOutput.additionalContext` from Stop. `decision: "block"` worked but showed as "Stop hook error" in the UI. | `jev_router.py:255-265`, test-results §2 |
| How do we get the recent turns the classifier needs? | The hook payload's `transcript_path`. Read the JSONL tail and take the last assistant text. | task-manager `pending_tasks.py:626-655` |
| What latency budget does inline classification have? | Jev's API: 438–660 ms, occasionally ~1.5 s, with a 2 s timeout that fails open. `claude -p` is too slow to run inline: task-manager runs its model calls detached because "the Stop hook has 5 s and a model call does not fit". | test-results §3; `pending_tasks.py:553-555` |
| How can "done" be detected without a test command? | task-manager runs a detached Haiku "judge". It reads Claude's closing message plus the user's next message, conservatively marks tasks done ("ok, merge" counts as confirmation), and detects replies to a question. | `pending_tasks.py:515-606` |

## 3. Lessons to borrow

- **Shadow mode first.** Log decisions without acting on them, then switch to active. (Jev `:232`, `:256`)
- **Fail open, and log it distinctly.** Any error or timeout means the message passes through as a steer. Jev's early end-to-end test "passed" only because a missing key meant every message silently went native. Only a null classifier field in the log showed it. (test-results §5)
- **Two signals for interrupt.** Jev requires both interrupt ≥ 0.9 *and* a separate "explicit stop" ≥ 0.9. Queue ≥ 0.9 also requires the stop signal to be < 0.5. (`:194-202`)
- **An explicit `unclear` class that falls back to steer.** This matches our "unresolvable → steer" rule. (`:150-155`)
- **Filter harness traffic.** System reminders and agent-completion notices arrive through the same hook. task-manager once turned 4 agent reports into 20 fake tasks. (`:102`, `:750-754`)
- **Skip non-tasks.** "ok", "yes", "continue", slash commands. A `.` accepts the suggested next step. (`:99-105`)
- **Paste guard.** A message of 150+ words or 12+ lines counts as one ask, whatever bullets it contains. (`:116-121`)
- **Guard against fake splits.** Each part must carry its own content words, and no two parts may be about the same thing. (`:463-477`)
- **Isolate any model call made from a hook.** Use `claude -p --setting-sources "" --strict-mcp-config --exclude-dynamic-system-prompt-sections` with its own system prompt. Otherwise the model answers the message instead of classifying it, and could trigger hooks recursively. (`:489-493`)
- **Spin guard.** If the queue looks identical across two stops, don't push again. (`:620-623`)
- **Store hygiene.** Use per-session file locks (Jev `:85`), atomic writes via tmp + `os.replace`, ids scoped per queue, and never look up an id across projects. That bug once closed another project's task. (`pending_tasks.py:242-256`)
- **Keep the answer, not just the ask.** File the closing message against the finished task. This is a cheap seed for our handoff note. (`:658-666`)

## 4. Risks the code surfaced

- **Automatic continuation can annoy.** task-manager's Stop hook used to roll straight into the next task; it was silenced "per user request" (`:678-693`). Our approval gate per plan addresses this, but continuation should be configurable per task or queue.
- **Pressing Esc fires no hook.** Jev drops leftover queued messages on the next turn and shows a notice (`:219-227`). We need a policy: pause the queue on Esc, don't drop it.
- **Text-only turns can't be interrupted.** A hard stop only happens at a tool boundary. (Jev README limitations)
- **Third-party classifier.** Jev sends every mid-turn message, plus the first 500 characters of the task, to TypeSafe.
- **Word-overlap matching misfires.** Running task-manager's own functions over our golden set:
  - **Amend:** right target in 2 of 4 cases. g16 ("skip the tests") was folded in as extra text rather than treated as a cancel.
  - **False fold:** g12 ("add a logout endpoint") was folded into the *login* task.
  - **Split:** 1 of 5. Mixed steer + queue messages (g09, g27, g46) never split. g20 failed because "login endpoint" is below the 3-word minimum.
  - So `amend` and `split` need a semantic classifier.

## 5. Gaps neither project covers (ours to build)

1. Inferring dependencies at intake. (Jev's queue is a plain FIFO; task-manager has a manual `bg --blocks`.)
2. Amend with a semantic target, including cancel and dependency rewiring (g27).
3. Mixed-intent split, e.g. steer + queue, or approve + queue. (Jev labels these `unclear`.)
4. `parked` (maybe-later) vs definite queue items.
5. Classifier context from the recent turn, so "this" and "that" can be resolved. (Jev sends only the task text.)
6. Plan flow: just-in-time planning, approval gate, handoff notes, re-planning stale child plans.
7. A labelled eval set with asymmetric scoring. Neither project has one.

## 6. Decisions to make

- **D1. Base.** (a) Fork Jev's router as the skeleton: its hook plumbing, `prompt_id` detection, shadow mode and interrupt path are clean and small. (b) Build fresh using these lessons. (c) Extend task-manager. Its non-blocking "queue everything and tell Claude" model is a different philosophy, and it's 4× larger.
- **D2. Classifier backend** (must answer in ~1–2 s): Anthropic API with Haiku called directly; a local model through Ollama on the lab PC (private, no per-call cost); or TypeSafe Jev (fastest to try, third-party).
- **D3. Dispatch default.** Run the next ready task automatically, or wait for approval of each plan.
- **D4. Done detection.** A verify command per task, a model judge on the closing message, or both (check if present, judge otherwise).
- **D5. Esc policy.** Pause the queue, or drop it with a notice.

## 7. Milestones (proposed)

- **M0. Shadow logger** (hooks only, no model). Log every prompt with `prompt_id`, the mid-turn flag, the transcript tail and the harness-traffic filter. Run it for a few days and harvest real messages into the golden set. *Spec by hand: the log record schema.*
- **M1. Classifier + eval.** A runner over `golden_queue_intake.jsonl` (exact label match, `also_acceptable`, asymmetric dependency scoring) and one backend from D2. Gate: hard dependency fails = 0, and all interrupt cases correct. *Spec by hand: the classifier's input/output contract.*
- **M2. Queue + dispatch.** Task store (states, `depends_on`, cycle check), Stop-hook handover via `additionalContext`, interrupt via `continue:false`, spin guard, Esc policy. *Spec by hand: the state machine.*
- **M3. Plan flow.** `plans/pN.md`, approval gate, `handoffs/pN.md`, re-planning stale children.
