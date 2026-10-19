# Queue harness: design spec (reconstructed)

> **Status.** Decisions D1 to D5 were made on 2026-10-18; see [decisions.md](decisions.md). Sections below that said "open" now state the decision and keep the alternatives for the record. The M2 plan flow (§3 state table, §4.3, §4.4, §7) was implemented on 2026-10-19 (`scripts/qsc/dispatch.py`, `intake.py`, `control.py`, `done.py`, `model.py`, `store.py`); those sections now describe the code, and the one deviation from the original design is where plans and handoffs live (§4.4).
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
| `planning` | Claude is writing the plan for the next task, in a Stop-hook continuation of the turn that just ended. | A message carrying the turn's own `prompt_id` is mid-turn, as in `running`. |
| `awaiting_approval` | A plan for the next task is waiting. | "lgtm" is `steer: approve`; "looks good but do X first" is `steer: revise plan`, never approve (g45); "go, and once it's in add logout" is `split` (g46). Intake checks this state before the non-task pass-through, so "ok" approves here and is a non-task everywhere else. An unrelated message runs as a normal turn and the plan is re-presented after it. |
| `awaiting_answer` | The running task ended its turn by asking the user something (judge verdict `asked_question`). | An approval word, alone or as the first clause ("ok, merge"), confirms the task done (§7). A queue marker queues. Anything else is the answer: the task resumes `running` and the message passes through untouched. No classifier call. |

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

Implemented in `scripts/qsc/dispatch.py` as one function per stage. Which stages run depends on the store state when Claude stops. The harness never writes a plan itself: the session's own Claude writes it, and dispatch steers it with handover texts that always start with `queue-switchclaude:` and name the task id and the absolute file it must read or write.

**A. Close the running task** (`_close_running`, state `running`):

1. The interrupt flag and a `paused` task are handled as before: an interrupt resets the store and dispatches nothing; a paused task keeps its status and only frees the running slot.
2. **Check** whether the task is done (`done.decide`, §7): its verify command if it has one, else the haiku judge over the hook's `last_assistant_message` (recommended by the docs over reading the transcript) and the first 1500 characters of its plan file.
3. `verify_failed` or `not_done`, fewer than 2 handbacks so far (`Task.attempts`): the turn is kept alive with `task pN failed its verify command (exit 3). Output tail: ... Fix it, then stop.` or `task pN does not look finished: <reason>. Finish it, then stop.` After the second handback the task is closed with verdict `gave_up` rather than nagged again, so the Stop-hook continuation cap (8) is never reached by the harness.
4. `asked_question`: the task stays `running`, the store becomes `awaiting_answer` with `pending_task = pN`, and the turn ends. Intake routes the user's next message (§3).
5. Otherwise (`verify_ok`, `done`, `stopped`, `gave_up`): **handoff**. `write_handoff` marks the task `done`, files the closing message and the verdict on it, and writes `handoffs/pN.md` (task text, notes, verdict and reason, the verify output tail when there is one, the closing message). A handoff file that cannot be written is logged and the task is still done.

**B. After a planning turn** (`_after_planning`, state `planning`): the plan file for `pending_task` exists and is non-empty, so the task becomes `planned`, `plan_based_on` records which tasks were `done` at that moment, and either the store becomes `awaiting_approval` and the turn ends with the plan on screen (decision D3), or, for an `auto` task, execution is handed over at once (**E**). No plan file: the planning handover is sent once more; a second empty planning turn returns the task to `queued`, the store to `idle`, and logs `plan_failed`, so a Claude that asked a question or hit an error instead of planning never loops.

**C. Waiting states**: a Stop in `awaiting_approval` or `awaiting_answer` changes nothing. The user's message either moved the store on (intake) or ran something unrelated, in which case intake had already made that a normal `running` turn.

**D. Choose what is next** (`choose_next`, from `idle` or after A closed a task):

1. The store goes to `idle` with no running or pending task. Every `planned` task whose plan predates the completion of one of its own dependencies (`Store.stale_planned`) goes back to `queued`, its plan file is deleted, and `replan` is logged.
2. `paused` and shadow mode as in M1: nothing is handed over; shadow logs what would run.
3. A `planned` task whose dependencies are all done is re-presented before anything new is planned: the store becomes `awaiting_approval` and the handover is `task pN is planned at <plan path> and waiting for your approval; present the plan briefly and stop.` This is how a plan whose approval an unrelated turn deferred comes back.
4. Else the first ready task (`Store.ready()`, all `depends_on` done, creation order) gets the **planning handover**: the store becomes `planning` with `pending_task = pN`, and the text is `next task is pN: <text>[. The user later added: <notes>]. Read the handoff of what came before at <handoff paths of its depends_on that exist, or "none">. Write a short plan for pN to <plan path> (goal, steps, how to verify), present it to the user in a few lines, then stop and wait for approval.` For an `auto` task the last clause is `then carry it out immediately without waiting.`; the state is still `planning`, and stage B hands execution over once the file is there.
5. **Spin guard**: a Stop fired by the harness's own continuation (`stop_hook_active`) whose next task is the one dispatched last pushes nothing.

**E. Execution handover** (`start`): the task becomes `running`, the store `running`, and the text is `task pN is approved. Execute the plan at <plan path> now. When it is done, say what was produced and stop.` Dispatch reaches it from B for an `auto` task; intake reaches it when the user approves (`lgtm`, `queue: approve`, or the classifier's `steer: approve`), in which case the text rides on the user's own message as `additionalContext`.

Every handover goes through `hookSpecificOutput.additionalContext`. `decision: "block"` also keeps the turn alive but shows as "Stop hook error" in the UI, so it is not used. The Stop hook timeout in `hooks/hooks.json` is 180 s, enough for a 120 s verify command plus the judge.

### 4.4 Store

Implemented in `scripts/qsc/store.py` (v3) and `scripts/qsc/paths.py`.

- One queue per session, per-session file locks (`fcntl.flock`), atomic writes via tmp + `os.replace`. A missing, unparseable or wrongly shaped file is a fresh store, and the corruption is logged. Files written by M0 and M1 load unchanged: new fields have defaults and unknown keys are ignored.
- Ids scoped per queue (`p1`, `p2`, ..., never reused); never look up an id across projects (that bug once closed another project's task).
- Store states: `idle`, `running`, `planning`, `awaiting_approval`, `awaiting_answer`. `pending_task` names the task being planned, awaiting approval or awaiting an answer; `running_task` the one executing.
- Task statuses: `queued`, `parked`, `planned`, `running`, `paused`, `done`, `interrupted`, `dropped`, `cancelled`, `failed_verify`, plus `shadow_queued` and `shadow_dispatched` in shadow mode. There is no stored `ready` status: `Store.ready()` computes it (queued, every dependency done). `failed_verify` is accepted by the store and by `queue: promote` and `cancel`, but dispatch on main never sets it: a failing verify command is handed back twice and then closed as `gave_up`.
- Task fields: `id`, `text`, `status`, `depends_on[]`, origin `prompt_id`, `created_at`, `norm_text` (dedupe), `notes[]` (amend appendices), and from M2 `verify` (shell command, decision D4), `auto` (skip the gate, D3), `attempts` (handbacks or planning retries so far, cap 2), `plan_based_on[]` (the `done` ids when the plan was written; `stale_planned()` compares it with the current `done` ids restricted to the task's own `depends_on`), `closing` (the message filed at done) and `verdict` (`verify_ok`, `verify_failed`, `done`, `not_done`, `asked_question`, `gave_up`, `stopped`, or `confirmed` when the user confirmed it).
- **Where plans and handoffs live (deviation from the original design).** Not in the user's repository: `paths.plan_path(session_id, task_id)` is `<data dir>/sessions/<session_id>/plans/<task_id>.md` and `paths.handoff_path(...)` is `.../handoffs/<task_id>.md`, next to the session's `<session_id>.json` store. The user's working tree stays clean; `queue: show pN` prints the paths that exist.
- Cycle check on every dependency write **(inferred, not implemented)**: a dependency that names no known task is simply never satisfied.

### 4.5 Model calls from hooks

Any model call made from a hook runs isolated:

```
claude -p --setting-sources "" --strict-mcp-config --exclude-dynamic-system-prompt-sections
```

with its own system prompt and `--json-schema` for the Decision contract. Otherwise the model answers the message instead of classifying it, and can trigger hooks recursively. This is the chosen classifier caller (decision D2b): it uses the user's Claude Code auth and costs 3 to 5 s per call, which the rules-first intake keeps rare. The judge on Stop uses the same call: `scripts/qsc/model.py` is the one wrapper (`model.call(prompt_path, schema, payload, timeout=15, model="haiku")`, exact flags in `model.FLAGS`, `None` on any failure), with `prompts/intake_system.md` and `prompts/judge_system.md` as the two prompts. Note: the official hooks reference gives command hooks a 600 s default timeout (30 s on `UserPromptSubmit`), so the "Stop hook has 5 s" figure from claude-task-manager is not a current constraint; `hooks/hooks.json` sets 25 s on `UserPromptSubmit` and 180 s on `Stop`. The 2 s budget is about the user waiting, not the hook dying.

## 5. Latency budget

Measured, not hoped: `claude -p` on haiku with isolation flags takes 3.6 to 5.5 s wall-clock on the development machine (2 to 4 s of it model time). The `UserPromptSubmit` hook timeout is 30 s, so the budget is about perceived delay, not failure. The rules in §4.1 step 5 exist to keep most messages off the model path; M0 measures what fraction they catch. The plan's original 1 to 2 s target came from a direct API call, which decision D2b rejected.

## 6. Policies

- **Shadow mode first.** Log decisions without acting on them, then switch to active.
- **Esc fires no hook.** Policy (decision D5): drop. The next message with a new `prompt_id` while state is `running` means the turn was interrupted: the task is marked `interrupted`, queued and ready tasks are dropped, and a notice listing them is attached to that prompt. Parked and done items survive.
- **Automatic continuation** can annoy. Continuation into the next task is configurable per task or per queue; the approval gate is the default protection.
- **Keep the answer, not just the ask.** The closing message is filed against the finished task and seeds the handoff note.

## 7. Done detection (decision D4)

Verify command if the task has one (done means exit 0); otherwise a haiku judge via `claude -p` over `last_assistant_message` and the plan, answering done / not done / asked a question, conservative. The user's next message can also confirm ("ok, merge"). The judge runs inline in the Stop hook (600 s budget, about 4 s).

As implemented in `scripts/qsc/done.py` (`decide(task, event, plan_excerpt)` returns a `Verdict(verdict, reason, output_tail)`):

- **Verify command** (`queue: verify pN <command>`): `run_verify` runs it through the shell in the Stop event's `cwd` with a 120 s timeout. Exit 0 is `verify_ok`; anything else is `verify_failed` with `reason = "exit N"` and the last 1500 characters of stdout+stderr as the `output_tail` that dispatch hands back. A command that outlives the timeout or cannot start is also `verify_failed`, so the Stop hook returns within its own budget and the task is never left `running` forever.
- **Judge** (`prompts/judge_system.md`, `JUDGE_SCHEMA` = `{verdict: done | not_done | asked_question, confidence, reason}`): the payload is the task text, its notes, the plan excerpt and the closing message; 15 s timeout. The prompt is conservative: `done` only when the closing message says the work is complete and names what was produced; a message that needs an answer to continue is `asked_question` even when most of the work is done; otherwise `not_done`. A `not_done` with confidence below 0.7 is treated as `done` (conservative in the direction of not nagging). A judge that is unavailable or answers outside its contract **fails open** as `done` with reason `judge unavailable; fail open`.
- An empty closing message is `stopped`, which dispatch counts as done.
- **Attempts.** `verify_failed` and `not_done` are handed back to Claude at most twice per task (`Task.attempts`); then the task is closed with verdict `gave_up`, and the handoff file says so.
- **User confirmation.** While the store is `awaiting_answer`, an approval word alone ("ok") or as the first clause ("ok, merge", but not "ok, but use postgres") closes the task as `confirmed` through the same `write_handoff` and plans the next task on that same message.

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
- **M2 Plan flow.** Just-in-time `plans/pN.md` from the previous task's handoff, the approval gate as the default with the `auto` override, `handoffs/pN.md`, re-planning a stale child, and done detection by verify command or the haiku judge. Implemented 2026-10-19 (§4.3, §4.4, §7; plan `superpowers/plans/2026-10-19-m2-plan-flow.md`), with the plan and handoff files under the plugin data dir rather than the repository.
- **M3 Hardening.** Multi-session store hygiene and id scoping, the spin guard, harness-traffic edge cases, the Stop-hook continuation cap, and whatever the M0 to M2 usage logs show.

## 11. Inference

- **Who runs the model.** Claude Code itself, through `claude -p`, on the user's own auth. No API key, no third-party service, no local server (decision D2b).
- **Which model.** The `haiku` alias for intake and for the done judge. If the M1 eval shows haiku missing the hard amend and split cases, raising the model is an M1 decision made with numbers.
- **What the model sees.** A fixed system prompt holding the class definitions and rules from §3 (written once, cached by the API), then one JSON payload: harness state, the running task, the queue with ids and intents, the last few turns from the transcript tail, and the message. The golden set's `rationale` fields are the prompt's worked examples.
- **What it returns.** JSON validated against the Decision schema via `--json-schema`: label, interrupt and parked flags, `depends_on`, amend `target`, split `parts`, confidence. The intake script re-validates it before acting.
- **Isolation.** `--setting-sources "" --strict-mcp-config --tools ""` plus the custom system prompt: no project context, no MCP servers, no tools, so the call cannot answer the message instead of classifying it or trigger hooks recursively.
- **When it is called.** Only when the rules in §4.1 cannot decide. Harness traffic, non-tasks, idle with an empty queue, and explicit markers never reach it. Measured 3.6 to 5.5 s wall-clock and about $0.0004 per call.
- **When it fails.** Timeout (15 s), parse error, schema mismatch or confidence below the threshold all mean `steer`: the message goes through as if the plugin were not installed, and the log marks the classifier field null so the miss is visible.
- **Call sites.** Intake on `UserPromptSubmit` for routing (M0), and the judge on `Stop` for done detection (M2). One wrapper (`scripts/qsc/model.py`), two prompts (`prompts/intake_system.md`, `prompts/judge_system.md`), two schemas (`classify.SCHEMA`, `done.JUDGE_SCHEMA`). The judge fails open as `done`, the classifier as `steer`.

## 12. Libraries

Per problem area, with code sketches: [libraries.md](libraries.md). After D2b the classifier section there is superseded by `claude -p`; the store, DAG, matching and eval picks stand.

## 13. Reference projects

- **Jev_steer_or_queue**: router, ~300 lines. Source of the hook plumbing, `prompt_id` detection, interrupt path, shadow mode and `docs/test-results.md`.
- **claude-task-manager**: queue, ~1,200 lines, `pending_tasks.py`. Source of the transcript-tail reader, detached Haiku judge, harness-traffic filter and store-hygiene lessons.

## 14. What was lost and needs rebuilding

1. The original wording of the design beyond what the plan and the golden set pin down.
2. The M0 log record schema, if it had been drafted.
