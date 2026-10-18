# Queue harness: design spec (reconstructed)

> **Status.** Decisions D1 to D5 were made on 2026-10-18; see [decisions.md](decisions.md). Sections below that said "open" now state the decision and keep the alternatives for the record.
>
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

Three hooks, one store, all Python, packaged as a Claude Code plugin (`hooks/hooks.json` with `command` hooks under `${CLAUDE_PLUGIN_ROOT}`; decision D2). See [flows.md](flows.md) for the diagrams.

### 4.1 Intake (`UserPromptSubmit`)

1. Receive the prompt. The hook fires immediately even for a message sent mid-run (verified on Claude Code 2.1.280).
2. Mid-turn vs new turn: compare `prompt_id`. A mid-turn message carries the running turn's id; a new turn gets a fresh one. Messages sent during a Stop-hook continuation keep the original id.
3. Drop harness traffic and non-tasks.
4. Read the transcript tail from the hook payload's `transcript_path` for the recent turns (the `recent` field in the golden set). The docs warn the transcript "may lag the in-memory conversation", so treat it as best-effort context.
5. Apply the deterministic rules (decision D2b): harness traffic and non-tasks pass through; idle with an empty queue passes through and records; explicit markers decide alone ("stop", `pN:`, `later:`, "maybe later", a numbered list, a lone `.`). Only what the rules cannot decide goes to the model.
6. Call the classifier: `claude -p --model haiku --json-schema <Decision>` with full isolation flags, 15 s timeout (measured 3.6 to 5.5 s wall-clock). Any error or timeout **fails open**: the message passes through as `steer`, and the log records a null classifier field so silent failure is visible.
7. Act on the class: steer passes through; queue/amend/parked/split write to the store and block the message; interrupt sets the flag and blocks.

### 4.2 Interrupt (`PreToolUse` / `PostToolUse`)

If the interrupt flag is set, return `{"continue": false}`. The Stop hook does not fire after that. Limitation: a text-only turn has no tool boundary, so it cannot be hard-stopped.

### 4.3 Dispatch (`Stop`)

1. **Check** whether the task that was running is done (see §7). The closing message comes from the hook's `last_assistant_message` field, which the docs recommend over reading the transcript.
2. **Handoff**: file that closing message against the finished task, and write `handoffs/pN.md`.
3. **Plan next**: pick the next ready task (all `depends_on` satisfied), write `plans/pN.md` from the handoff note just in time. If a child's plan was written earlier and the parent's outcome changed, re-plan it.
4. **Approve** (decision D3): the gate is on by default. Claude presents the plan and the turn ends with state `awaiting_approval`; "lgtm" runs it, "looks good but X first" revises it. A task marked `auto`, or a message like "run it without asking", skips the gate.
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

with its own system prompt and `--json-schema` for the Decision contract. Otherwise the model answers the message instead of classifying it, and can trigger hooks recursively. This is the chosen classifier caller (decision D2b): it uses the user's Claude Code auth and costs 3 to 5 s per call, which the rules-first intake keeps rare. The judge on Stop uses the same call. Note: the official hooks reference gives command hooks a 600 s default timeout (30 s on `UserPromptSubmit`), so the "Stop hook has 5 s" figure from claude-task-manager is not a current constraint. The 2 s budget is about the user waiting, not the hook dying.

## 5. Latency budget

Measured, not hoped: `claude -p` on haiku with isolation flags takes 3.6 to 5.5 s wall-clock on the development machine (2 to 4 s of it model time). The `UserPromptSubmit` hook timeout is 30 s, so the budget is about perceived delay, not failure. The rules in §4.1 step 5 exist to keep most messages off the model path; M0 measures what fraction they catch. The plan's original 1 to 2 s target came from a direct API call, which decision D2b rejected.

## 6. Policies

- **Shadow mode first.** Log decisions without acting on them, then switch to active.
- **Esc fires no hook.** Policy (decision D5): drop. The next message with a new `prompt_id` while state is `running` means the turn was interrupted: the task is marked `interrupted`, queued and ready tasks are dropped, and a notice listing them is attached to that prompt. Parked and done items survive.
- **Automatic continuation** can annoy. Continuation into the next task is configurable per task or per queue; the approval gate is the default protection.
- **Keep the answer, not just the ask.** The closing message is filed against the finished task and seeds the handoff note.

## 7. Done detection (decision D4)

Verify command if the task has one (done means exit 0); otherwise a haiku judge via `claude -p` over `last_assistant_message` and the plan, answering done / not done / asked a question, conservative. The user's next message can also confirm ("ok, merge"). The judge runs inline in the Stop hook (600 s budget, about 4 s).

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

## 9. Decisions

All five are made; the record with evidence and rejected options is [decisions.md](decisions.md).

| | Decision |
|---|---|
| D1 | Build fresh. |
| D2 | Classic hooks, all Python, packaged as a plugin. Mod route kept as the upgrade path. |
| D2b | Classifier via `claude -p --model haiku --json-schema`, rules first. |
| D3 | Approve each plan; per-task `auto` override. |
| D4 | Verify command if set, else haiku judge; user confirmation also counts. |
| D5 | Esc drops queued and ready tasks with a notice; parked and done survive. |

## 10. Milestones (working product first)

Decided 2026-10-18: the first milestone is a thin end-to-end slice a real session can use, not a logger. Shadow mode is a switch inside it.

- **M0 Vertical slice.** One session, the full loop on the unambiguous cases. Classes: `steer`, `queue`, `interrupt` only. Dependencies: a plain chain (each new task depends on the last queued one, or on the running one). Dispatch: when Claude stops, the next ready task is handed over as `additionalContext`; no plans, no approval gate, done means Claude stopped. Interrupt: flag on intake, `continue: false` on the next tool call. Classification: rules first, then `claude -p`; a message is queued only when a rule matches (`pN:`, `later:`, "after that", "once that's done", "when you're done") or the model answers `queue` with confidence ≥ 0.9. Everything else passes through. A `shadow` setting logs decisions without acting. Store: one JSON file per session with a lock and atomic writes. Deliverable: install the plugin, type a task mid-run, watch it run after the current one, say "stop" and see the turn end.
- **M1 Make it trustworthy.** Eval runner over the golden set with the asymmetric dependency score; the Decision contract tightened with the rationales as worked examples; `amend` (rapidfuzz, then embeddings, then the model picks the target) and `split` with the fake-split guard; the `parked` list and promotion; mixed-intent cases g09, g27, g46; Esc detection and the drop-with-notice policy; a `/queue` command to list, cancel and promote. Gate: zero missed hard dependencies, every interrupt case correct, shadow-mode disagreement rate reviewed.
- **M2 Plan flow.** Just-in-time `plans/pN.md` from the previous task's handoff, the approval gate as the default with the `auto` override, `handoffs/pN.md`, re-planning a stale child, and done detection by verify command or the haiku judge.
- **M3 Hardening.** Multi-session store hygiene and id scoping, the spin guard, harness-traffic edge cases, the Stop-hook continuation cap, and whatever the M0 to M2 usage logs show.

## 11. Inference

- **Who runs the model.** Claude Code itself, through `claude -p`, on the user's own auth. No API key, no third-party service, no local server (decision D2b).
- **Which model.** The `haiku` alias for intake and for the done judge. If the M1 eval shows haiku missing the hard amend and split cases, raising the model is an M1 decision made with numbers.
- **What the model sees.** A fixed system prompt holding the class definitions and rules from §3 (written once, cached by the API), then one JSON payload: harness state, the running task, the queue with ids and intents, the last few turns from the transcript tail, and the message. The golden set's `rationale` fields are the prompt's worked examples.
- **What it returns.** JSON validated against the Decision schema via `--json-schema`: label, interrupt and parked flags, `depends_on`, amend `target`, split `parts`, confidence. The intake script re-validates it before acting.
- **Isolation.** `--setting-sources "" --strict-mcp-config --tools ""` plus the custom system prompt: no project context, no MCP servers, no tools, so the call cannot answer the message instead of classifying it or trigger hooks recursively.
- **When it is called.** Only when the rules in §4.1 cannot decide. Harness traffic, non-tasks, idle with an empty queue, and explicit markers never reach it. Measured 3.6 to 5.5 s wall-clock and about $0.0004 per call.
- **When it fails.** Timeout (15 s), parse error, schema mismatch or confidence below the threshold all mean `steer`: the message goes through as if the plugin were not installed, and the log marks the classifier field null so the miss is visible.
- **Call sites.** Intake on `UserPromptSubmit` for routing (M0), and the judge on `Stop` for done detection (M2). One wrapper, two prompts, two schemas.

## 12. Libraries

Per problem area, with code sketches: [libraries.md](libraries.md). After D2b the classifier section there is superseded by `claude -p`; the store, DAG, matching and eval picks stand.

## 13. Reference projects

- **Jev_steer_or_queue**: router, ~300 lines. Source of the hook plumbing, `prompt_id` detection, interrupt path, shadow mode and `docs/test-results.md`.
- **claude-task-manager**: queue, ~1,200 lines, `pending_tasks.py`. Source of the transcript-tail reader, detached Haiku judge, harness-traffic filter and store-hygiene lessons.

## 14. What was lost and needs rebuilding

1. The original wording of the design beyond what the plan and the golden set pin down.
2. The M0 log record schema, if it had been drafted.
