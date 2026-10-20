# queue-switchclaude: design spec

> **What this document is.** The design of the plugin as built, at version 0.4.1 (2026-10-19). Sections 3, 4 and 7 describe the code in `scripts/qsc/` and name the function that implements each stage; sections 10 and 11 record the milestones and the inference choices. The decisions behind it, with the evidence and the rejected options, are in [decisions.md](decisions.md) (D1 to D8); the diagrams are in [flows.md](flows.md); the audit that checked this spec against the code is [2026-10-19-readme-audit.md](2026-10-19-readme-audit.md).
>
> **Provenance.** The Claude Code session that produced the original design and the 47-case golden set was deleted on 2026-10-18 before anything was committed. The only file that survived is [`queue-harness-plan.md`](../queue-harness-plan.md) (the "side plan", written 2026-10-18 17:50); this spec was first rebuilt from that file and then rewritten section by section as each milestone was implemented. The golden set was recovered separately on 2026-10-18 and lives at [`eval/golden_queue_intake.jsonl`](../eval/golden_queue_intake.jsonl).

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
| `interrupt` (flag on `steer`) | Stop the current work now. | Block the message, set a flag, return `{"continue": false}` from the next `PreToolUse` hook. Cases g29, g30. |
| `parked` (flag on `queue`) | A maybe-later item ("maybe later we can add social login", g33), not a definite queue entry. | Stored with its dependencies but never dispatched until the user promotes it. "later: email verification" (g35) is a definite queue item, not parked. |
| `unclear` | Classifier cannot decide, or a referent cannot be resolved ("maybe later we can add this", g34). | Falls back to `steer`: Claude has the context to ask; the harness must never invent a task with no content. |

`steer` also carries a `note` in some states: `approve` or `revise plan` while a plan awaits approval (g44, g45, g47), `pause p1` for a reprioritisation (g27). `amend` carries `note: cancel` when it removes a task (g16, g43).

**Harness state changes what words mean.** The classifier always sees the state:

| State | Meaning | Effect on classification |
|---|---|---|
| `running` | A task is executing. | Corrections, constraints, answers to Claude's questions and status questions are `steer`. New scope is `queue`. |
| `idle` | Nothing running, queue empty or all done. | Anything actionable is `queue` and runs at once if its dependencies are done, even if phrased as a correction ("the user model's missing a unique constraint", g18). Questions stay `steer` (g19). The rule markers change meaning here (M3, §4.1 step 5): `later: x` runs `x` now with the marker stripped, `maybe later x` parks it and nothing runs, `stop` is a no-op answered with `nothing is running`. |
| `planning` | Claude is writing the plan for the next task, in a Stop-hook continuation of the turn that just ended. | A message carrying the turn's own `prompt_id` is mid-turn, as in `running`. A message with a new `prompt_id` means Esc cut the planning turn: the task returns to `queued`, its partial plan is deleted, nothing is dropped, and the message runs as a normal turn (§6). |
| `awaiting_approval` | A plan for the next task is waiting. | "lgtm" is `steer: approve`; "looks good but do X first" is `steer: revise plan`, never approve (g45); "go, and once it's in add logout" is `split` (g46). Intake checks this state before the non-task pass-through, so "ok" approves here and is a non-task everywhere else. An unrelated message runs as a normal turn and the plan is re-presented after it. |
| `awaiting_answer` | The running task ended its turn by asking the user something (judge verdict `asked_question`). | An approval word, alone or as the first clause ("ok, merge"), confirms the task done (§7). A queue marker queues. Anything else is the answer: the task resumes `running` and the message passes through untouched. No classifier call. |

Classification rules carried over from the two reference projects:

- **Interrupt is explicit.** A rule matches a whole message of `stop`, `halt` or `abort` (optionally `that`, `this`, `it`, `now`, `everything`) or `don't do that`; otherwise the model's single `interrupt` flag at confidence ≥ 0.9 (`intake.py`). The original design's two-score rule (interrupt ≥ 0.9 and a separate explicit-stop score) survives only as prose in the classifier prompt; there is no `explicit_stop` field in the schema.
- **Skip non-tasks.** "ok", "yes", "continue", slash commands. A lone `.` accepts the suggested next step.
- **Paste guard.** 150+ words or 12+ lines skips the rule table and goes to the model whole. The model may still answer `split`; nothing in the code forces one ask.
- **Fake-split guard.** In code: empty parts and parts whose normalised text duplicates another are dropped (`intake.py`), and each part is gated at confidence 0.9. The rule that each part must carry its own content words lives in the classifier prompt, not in code.
- **Filter harness traffic.** System reminders and agent-completion notices arrive through the same hook and must never become tasks.
- **Classifier context.** The classifier sees the recent turn (last assistant and user text from the transcript tail), so "this", "that", "same" and "nvm" resolve. The same words flip class with context: "this is necessary" answers a question in g31 but is a bare acknowledgement in g32; "nvm" undoes an applied steer in g42 but cancels a queued task in g43.

## 4. Architecture

```
message ─▶ UserPromptSubmit: INTAKE (classify) ─▶ QUEUE / DAG
Claude stops ─▶ Stop: DISPATCH (check → handoff → plan next → approve) ─▶ next task
any tool call ─▶ PreToolUse: INTERRUPT check
```

Three hooks (`UserPromptSubmit`, `Stop`, `PreToolUse`; decision D6 dropped the `PostToolUse` entry), one store, all Python, packaged as a Claude Code plugin (`hooks/hooks.json` with `command` hooks under `${CLAUDE_PLUGIN_ROOT}`; decision D2). See [flows.md](flows.md) for the diagrams.

### 4.1 Intake (`UserPromptSubmit`)

1. Receive the prompt. The hook fires immediately even for a message sent mid-run (verified on Claude Code 2.1.280).
2. Mid-turn vs new turn: compare `prompt_id`. A mid-turn message carries the running turn's id; a new turn gets a fresh one. Messages sent during a Stop-hook continuation keep the original id.
3. Drop harness traffic and non-tasks. Harness traffic is any message that starts with `<` or carries one of `rules.HARNESS_PHRASES` in its first 200 characters (system reminders, `<task-notification>`, the `<command-message>` and `<command-name>` wrappers of a skill or slash command, `[Request interrupted by user`, `Base directory for this skill:`, side-session and CI-monitor events). It passes through before the control rule and before the store is read, so it can never become a task, a dedupe hit or a control command, in any state (M3).
4. Read the transcript tail from the hook payload's `transcript_path` for the recent turns (the `recent` field in the golden set). The docs warn the transcript "may lag the in-memory conversation", so treat it as best-effort context.
5. Apply the deterministic rules (decision D2b): harness traffic and non-tasks pass through; idle with an empty queue passes through and records; explicit markers decide alone: `stop` (and `halt`, `abort`, `don't do that`); the queue markers `pN:`, `later:`, `then …`, `after that …`, `once/when/after that's done …`; the parked markers `maybe later`, `someday`, `at some point`, `one day`, `if we have time` (a bare-pronoun remainder such as "maybe later we can add this", g34, is sent to the model instead); a lone `.` and the other non-task words. A numbered list is only named in the log (`numbered_split`); the model does the split. Only what the rules cannot decide goes to the model. At `idle` (nothing running, nothing pending) the markers mean something else (M3, `intake._idle_turn`): `later: x` has nothing to wait for, so `x` (the remainder, not the raw prompt) becomes the running turn's text and the prompt passes through with `queue-switchclaude: nothing is running, so this starts now: x` (log `decision: new_turn, marker_stripped: true`); `maybe later x` is parked and blocked, nothing runs (`parked_idle`); `stop` is blocked with `nothing is running`, no task and no flag (`noop`). Dedupe still runs first, so held marker text sent again at idle is caught.
6. Call the classifier: `claude -p --model haiku --json-schema <Decision>` with full isolation flags, 15 s timeout (measured 3.6 to 5.5 s wall-clock). Any error or timeout **fails open**: the message passes through as `steer`, and the log records a null classifier field so silent failure is visible.
7. Act on the class: steer passes through; queue/amend/parked/split write to the store and block the message; interrupt sets the flag and blocks.

### 4.2 Interrupt (`PreToolUse`)

Registered on `PreToolUse` only (decision D6); since 0.4.0 the script returns before reading the store for any other event. The hook peeks at the store without the lock on every tool call; when the interrupt flag is set it takes the lock, applies the same reset as Esc (`interrupt.reset_turn`, §6: while a task runs it is marked `interrupted` and `queued` tasks are dropped, D5; while a plan is written the task being planned is queued again; a `planned` task waiting for approval is kept either way), and returns `{"continue": false}` with a `stopReason` naming what was dropped or requeued. The Stop hook does not fire after that. An `AskUserQuestion` call is skipped without reading the store: the turn is already waiting on the user, and ending it there would only swallow the question. A text-only turn has no tool boundary, so it cannot be hard-stopped; the flag is then honoured at Stop, where dispatch applies the same reset and dispatches nothing, so a stale flag never ends the next turn at its first tool call.

### 4.3 Dispatch (`Stop`)

Implemented in `scripts/qsc/dispatch.py` as one function per stage. Which stages run depends on the store state when Claude stops. The harness never writes a plan itself: the session's own Claude writes it, and dispatch steers it with handover texts that always start with `queue-switchclaude:` and name the task id and the absolute file it must read or write.

**A. Close the running task** (`_close_running`, state `running`):

1. The interrupt flag and a `paused` task are handled as before: an interrupt resets the store and dispatches nothing; a paused task keeps its status and only frees the running slot.
2. **Check** whether the task is done (`done.decide`, §7): its verify command if it has one, else the haiku judge over the hook's `last_assistant_message` (recommended by the docs over reading the transcript) and the first 1500 characters of its plan file. The verify command or judge runs *before* the store lock is taken (`_precompute_verdict`), so a 120 s verify or a 4 s judge no longer makes a mid-run message wait on the lock (M2 acceptance, issue 1); the locked stage decides again only if the running task or its verify command changed meanwhile. A turn the user typed (`Task.kind == "prompt"`) is not judged: it closes as `stopped` with reason `prompt turn ended` and no model call, unless it carries a verify command (decision D8).
3. `verify_failed` or `not_done`, fewer than 2 handbacks so far (`Task.attempts`): the turn is kept alive with `task pN failed its verify command (exit 3). Output tail: ... Fix it, then stop.` (the `Output tail:` clause is omitted when the command printed nothing) or `task pN does not look finished: <reason>. Finish it, then stop.` After the second handback a `not_done` task is closed with verdict `gave_up` rather than nagged again; a `verify_failed` task is instead set aside with status `failed_verify` (not done, so nothing that depends on it becomes ready; no handoff is written) and the user sees `queue-switchclaude: pN failed its verify command 2 times and is set aside as failed_verify (send 'queue: promote pN' to retry it, 'queue: cancel pN' to drop it)` as a `systemMessage`. Either way the Stop-hook continuation cap (8) is never reached by the harness.
4. `asked_question`: the task stays `running`, the store becomes `awaiting_answer` with `pending_task = pN`, and the turn ends. Intake routes the user's next message (§3).
5. Otherwise (`verify_ok`, `done`, `stopped`, `gave_up`): **handoff**. `write_handoff` marks the task `done`, files the closing message and the verdict on it, and writes `handoffs/pN.md` (task text, notes, verdict and reason, the verify output tail when there is one, the closing message). A handoff file that cannot be written is logged and the task is still done.

**B. After a planning turn** (`_after_planning`, state `planning`): the plan file for `pending_task` exists and is non-empty, so the task becomes `planned`, `plan_based_on` records which tasks were `done` at that moment, and either the store becomes `awaiting_approval` and the turn ends with the plan on screen (decision D3), or, for an `auto` task, execution is handed over at once (**E**), unless the continuation budget (D.6) is spent, in which case the store waits for approval and says so. A pending task the user cancelled, parked or dropped during the planning turn (control works in every state) is not resurrected: its plan file is ignored, `plan_failed` is logged and stage D moves on. No plan file: the planning handover is sent once more; a second empty planning turn returns the task to `queued`, the store to `idle`, and logs `plan_failed`, so a Claude that asked a question or hit an error instead of planning never loops. Esc during a planning turn never reaches this stage: intake handles it on the next prompt (§6).

**C. Waiting states**: a Stop in `awaiting_approval` or `awaiting_answer` changes nothing. The user's message either moved the store on (intake) or ran something unrelated, in which case intake had already made that a normal `running` turn.

**D. Choose what is next** (`choose_next`, from `idle` or after A closed a task):

1. The store goes to `idle` with no running or pending task. Every `planned` task whose plan predates the completion of one of its own dependencies (`Store.stale_planned`) goes back to `queued`, its plan file is deleted, and `replan` is logged.
2. `paused` and shadow mode as in M1: nothing is handed over; shadow logs what would run.
3. A `planned` task whose dependencies are all done is re-presented before anything new is planned: the store becomes `awaiting_approval` and the handover is `task pN is planned at <plan path> and waiting for your approval; present the plan briefly and stop.` This is how a plan whose approval an unrelated turn deferred comes back.
4. Else the first ready task (`Store.ready()`, all `depends_on` done, creation order) gets the **planning handover**: the store becomes `planning` with `pending_task = pN`, and the text is `next task is pN: <text>[. The user later added: <notes>]. Read the handoff of what came before at <handoff paths of its depends_on that exist, or "none">. Write a short plan for pN to <plan path> (goal, steps, how to verify), present it to the user in a few lines, then stop and wait for approval.` For an `auto` task the last clause is `then carry it out immediately without waiting.`; the state is still `planning`, and stage B hands execution over once the file is there.
5. **Spin guard** (`_issue`, M3): every handover text dispatch issues is hashed (sha1) into `Store.last_handback`. A Stop fired by the harness's own continuation (`stop_hook_active`) whose handover hashes equal to the last one is a spin: the handover is replaced by the `systemMessage` `queue-switchclaude: the same instruction came back twice; stopping here, send any message to continue`, the line is logged with `spin_guard: true`, and the turn ends with the store in the state the stage set (a planning spin stays `planning`, and intake requeues the task on the user's next prompt, §6). The "fix it" and "finish it" handbacks are bounded by `Task.attempts` instead, since their second copy is by design. The M1 guard on `last_dispatched` could never fire and is gone; the field is kept so older store files load.
6. **Continuation budget** (`MAX_CONTINUATIONS`, M3): `Store.continuations` counts consecutive Stop-hook continuations and is reset by any Stop that is not one. At 6 (under Claude Code's own cap of 8) dispatch stops driving the chain: a ready task is not planned, an `auto` task's fresh plan waits for approval instead of executing, a "fix it" or "finish it" handback is withheld (the task waits in `awaiting_answer`, so any message resumes it and "ok" confirms it done), a planning retry is not sent (the task goes back to `queued`), and the user sees `queue-switchclaude: paused the automatic chain after 6 continuations; send any message to carry on`.

**E. Execution handover** (`start`): the task becomes `running`, the store `running`, and the text is `task pN is approved. Execute the plan at <plan path> now. When it is done, say what was produced and stop.` For an `auto` task the text is `task pN is approved. Execute the plan at <plan path> now if you have not already carried it out; if you have, say so and stop.`: its planning handover already told Claude to carry on, so the second instruction has to be idempotent (M2 acceptance, issue 2). Dispatch reaches it from B for an `auto` task; intake reaches it when the user approves (`lgtm`, `queue: approve`, or the classifier's `steer: approve`), in which case the text rides on the user's own message as `additionalContext`.

Every handover goes through `hookSpecificOutput.additionalContext`. `decision: "block"` also keeps the turn alive but shows as "Stop hook error" in the UI, so it is not used. The Stop hook timeout in `hooks/hooks.json` is 180 s, enough for a 120 s verify command plus the judge.

**Housekeeping.** The first Stop of a session stamps `Store.gc_at` and runs `gc.collect(protect={session_id})` once (§4.4). The stamp is written before the call, so a collector that raises is not retried every turn, and every error is swallowed: housekeeping never costs the user a handover.

### 4.4 Store

Implemented in `scripts/qsc/store.py` (v3) and `scripts/qsc/paths.py`.

- One queue per session, per-session file locks (`fcntl.flock`), atomic writes via tmp + `os.replace`. A missing, unparseable or wrongly shaped file is a fresh store, and the corruption is logged. Files written by M0 and M1 load unchanged: new fields have defaults and unknown keys are ignored.
- Ids scoped per queue (`p1`, `p2`, ..., never reused); never look up an id across projects (that bug once closed another project's task).
- Store states: `idle`, `running`, `planning`, `awaiting_approval`, `awaiting_answer`. `pending_task` names the task being planned, awaiting approval or awaiting an answer; `running_task` the one executing.
- Task statuses: `queued`, `parked`, `planned`, `running`, `paused`, `done`, `interrupted`, `dropped`, `cancelled`, `failed_verify`, plus `shadow_queued` and `shadow_dispatched` in shadow mode. There is no stored `ready` status: `Store.ready()` computes it (queued, every dependency done). `failed_verify` is set by dispatch when a verify command failed on both handbacks (§4.3 A.3): the task is set aside, nothing that depends on it becomes ready, `queue: promote pN` (or `queue: resume pN`) returns it to `queued` and `queue: cancel pN` drops it; `queue: list` shows it with the other live tasks.
- Task fields: `id`, `text`, `status`, `depends_on[]`, origin `prompt_id`, `created_at`, `norm_text` (dedupe), `notes[]` (amend appendices), and from M2 `verify` (shell command, decision D4), `auto` (skip the gate, D3), `attempts` (handbacks or planning retries so far, cap 2), `plan_based_on[]` (the `done` ids when the plan was written; `stale_planned()` compares it with the current `done` ids restricted to the task's own `depends_on`), `closing` (the message filed at done) and `verdict` (`verify_ok`, `verify_failed`, `done`, `not_done`, `asked_question`, `gave_up`, `stopped`, or `confirmed` when the user confirmed it; the list is `done.VERDICTS`). From M3, `kind`: `prompt` for a turn the user typed, `task` for work the harness queued; only tasks are judged (decision D8).
- Store fields beyond `state`, `current_prompt_id`, `running_task`, `pending_task`, `next_id`, `interrupt` and `paused`: from M3 `continuations` (consecutive Stop-hook continuations, §4.3 D.6), `last_handback` (sha1 of the last handover text, the spin guard) and `gc_at` (when garbage collection ran on this session's behalf). `last_dispatched` (M0) is no longer read and is kept so older files load.
- **Where plans and handoffs live (decision D7, a deviation from the original design).** Not in the user's repository: `paths.plan_path(session_id, task_id)` is `<data dir>/sessions/<session_id>/plans/<task_id>.md` and `paths.handoff_path(...)` is `.../handoffs/<task_id>.md`, next to the session's `<session_id>.json` store. The user's working tree stays clean; `queue: show pN` prints the paths that exist. The data dir is `$CLAUDE_PLUGIN_DATA`, else the newest `~/.claude/plugins/data/queue-switchclaude*` directory (a command run by the Bash tool has no hook environment), else `~/.claude/queue-switchclaude`.
- **Session pointers (M3).** Intake writes, atomically and best effort on every event, `<data dir>/last_session` (the newest session anywhere) and `<data dir>/pointers/<sha1(cwd)[:16]>` (the newest session in that working directory, from the event's `cwd`). `scripts/queue_cli.py` resolves its session as `--session`, else `$QSC_SESSION`, else the pointer for the directory it runs in (the Bash tool runs in the project directory), else the global pointer; two sessions in different folders never see each other's queue (M3 review focus 1).
- **Garbage collection (M3, `scripts/qsc/gc.py`).** `gc.collect(keep_days=30, now=None, protect=frozenset())` deletes the store, its lock, the log and the session directory of every session whose newest file is older than the window, never a session in `protect`, never anything younger (the newest file decides, so one fresh plan keeps an old store, and a clock wrong by minutes cannot matter against a window of days). Pointer files that still name a deleted session go with it. Dispatch runs it once per session, with its own session protected, and swallows every error (§4.3, housekeeping).
- There is no cycle check on dependency writes. The model's `depends_on` names existing ids in every golden case, and a dependency that names no known task is simply never satisfied, so a bad write cannot loop; it can only leave a task waiting until `queue: cancel pN` or `queue: promote pN` is typed.

### 4.5 Model calls from hooks

Any model call made from a hook runs isolated:

```
claude -p --model haiku --setting-sources "" --strict-mcp-config --tools "" --output-format json --json-schema <schema> --system-prompt <prompt> <payload>
```

with its own system prompt and `--json-schema` for the Decision contract. Otherwise the model answers the message instead of classifying it, and can trigger hooks recursively. This is the chosen classifier caller (decision D2b): it uses the user's Claude Code auth and costs 3 to 5 s per call, which the rules-first intake keeps rare. The judge on Stop uses the same call: `scripts/qsc/model.py` is the one wrapper (`model.call(prompt_path, schema, payload, timeout=15, model="haiku")`, exact flags in `model.FLAGS`, `None` on any failure), with `prompts/intake_system.md` and `prompts/judge_system.md` as the two prompts. Note: the official hooks reference gives command hooks a 600 s default timeout (30 s on `UserPromptSubmit`), so the "Stop hook has 5 s" figure from claude-task-manager is not a current constraint; `hooks/hooks.json` sets 25 s on `UserPromptSubmit` and 180 s on `Stop`. The classifier timeout is 15 s; the figure that matters is the 3.4 to 4.7 s the user waits. Every hook launch is measured (M3): `hookio.read_event` stamps the event with `_hook_started`, intake, dispatch and the interrupt hook log `hook_ms` on every line, dispatch logs the judge's `latency_ms` (or a verify command's `verify_ms`) on its first `dispatch` line, and `eval/shadow_report.py` prints p50 and p95 per hook, split by whether a model call was made, next to the model's own `latency_ms` and a count per intake `decision` value.

## 5. Latency budget

Measured, not hoped: `claude -p` on haiku with isolation flags takes 3.6 to 5.5 s wall-clock on the development machine (2 to 4 s of it model time). The `UserPromptSubmit` hook timeout is 25 s in `hooks/hooks.json` (Claude Code's own default is 30 s), so the budget is about perceived delay, not failure. The rules in §4.1 step 5 exist to keep most messages off the model path; M0 measures what fraction they catch. The plan's original 1 to 2 s target came from a direct API call, which decision D2b rejected.

## 6. Policies

- **Shadow mode first.** Log decisions without acting on them, then switch to active.
- **Esc fires no hook.** Policy (decision D5): drop. The next message with a new `prompt_id` while state is `running` means the turn was interrupted: the task is marked `interrupted`, queued and ready tasks are dropped, and a notice listing them is attached to that prompt. Parked and done items survive. **Esc during planning** (M3): a new `prompt_id` while the state is `planning` means Esc cut the planning turn, not work, so nothing is dropped: the task being planned returns to `queued` with `attempts` reset, its partial plan file is deleted, `pending_task` is cleared, the log line carries `interrupted_planning: pN`, and the notice attached to the new prompt is `queue-switchclaude: the planning turn for pN was interrupted; it is queued again`. The new prompt runs as a normal turn and dispatch plans the task again after it. A task the user cancelled, parked or dropped during the planning turn is left as it is. A new prompt while a plan or a question waits (`awaiting_approval`, `awaiting_answer`) interrupts nothing and drops nothing either.
- **Automatic continuation** can annoy. Continuation into the next task is configurable per task or per queue; the approval gate is the default protection.
- **Keep the answer, not just the ask.** The closing message is filed against the finished task and seeds the handoff note.

## 7. Done detection (decision D4)

Verify command if the task has one (done means exit 0); otherwise a haiku judge via `claude -p` over `last_assistant_message` and the plan, answering done / not done / asked a question, conservative. The user's next message can also confirm ("ok, merge"). The judge runs inline in the Stop hook (180 s timeout in `hooks/hooks.json`, about 4 s used).

As implemented in `scripts/qsc/done.py` (`decide(task, event, plan_excerpt)` returns a `Verdict(verdict, reason, output_tail)`):

- **Verify command** (`queue: verify pN <command>`): `run_verify` runs it through the shell in the Stop event's `cwd` with a 120 s timeout. Exit 0 is `verify_ok`; anything else is `verify_failed` with `reason = "exit N"` and the last 1500 characters of stdout+stderr as the `output_tail` that dispatch hands back. A command that outlives the timeout or cannot start is also `verify_failed`, so the Stop hook returns within its own budget and the task is never left `running` forever. The shell starts in its own session (`start_new_session=True`) and on timeout its whole process group is killed (`os.killpg`), so a verify that spawned a server or a background job does not outlive the hook (M3).
- **Judge** (`prompts/judge_system.md`, `JUDGE_SCHEMA` = `{verdict: done | not_done | asked_question, confidence, reason}`): the payload is the task text, its notes, the plan excerpt and the closing message; 15 s timeout. The prompt is conservative: `done` only when the closing message says the work is complete and names what was produced; a message that needs an answer to continue is `asked_question` even when most of the work is done; otherwise `not_done`. A `not_done` with confidence below 0.7 is treated as `done` (conservative in the direction of not nagging). A judge that is unavailable or answers outside its contract **fails open** as `done` with reason `judge unavailable; fail open`.
- An empty closing message is `stopped`, which dispatch counts as done.
- **Attempts.** `verify_failed` and `not_done` are handed back to Claude at most twice per task (`Task.attempts`); then a `not_done` task is closed with verdict `gave_up` (the handoff file says so) and a `verify_failed` task is set aside as `failed_verify` with a `systemMessage` naming `queue: promote pN` (§4.3 A.3).
- **Prompt turns.** A turn the user typed (`Task.kind == "prompt"`) is never judged: it closes as `stopped`, reason `prompt turn ended`, unless it has a verify command (decision D8).
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

All eight are made; the record with evidence and rejected options is [decisions.md](decisions.md). D6 to D8 were decided on 2026-10-19 from the M2 and M3 acceptance runs.

| | Decision |
|---|---|
| D1 | Build fresh. |
| D2 | Classic hooks, all Python, packaged as a plugin. Mod route kept as the upgrade path. |
| D2b | Classifier via `claude -p --model haiku --json-schema`, rules first. |
| D3 | Approve each plan; per-task `auto` override. |
| D4 | Verify command if set, else haiku judge; user confirmation also counts. |
| D5 | Esc drops queued and ready tasks with a notice; parked and done survive. Esc during planning requeues the task and drops nothing. |
| D6 | The interrupt hook runs on `PreToolUse` only; `AskUserQuestion` is skipped; the flag is also honoured at Stop. |
| D7 | Plans and handoffs live under the plugin data dir, not in the user's repository. |
| D8 | Prompt turns are registered as tasks but never judged; they close as `stopped`. |

## 10. Milestones (working product first)

Decided 2026-10-18: the first milestone is a thin end-to-end slice a real session can use, not a logger. Shadow mode is a switch inside it.

- **M0 Vertical slice.** One session, the full loop on the unambiguous cases. Classes: `steer`, `queue`, `interrupt` only. Dependencies: a plain chain (each new task depends on the last queued one, or on the running one). Dispatch: when Claude stops, the next ready task is handed over as `additionalContext`; no plans, no approval gate, done means Claude stopped. Interrupt: flag on intake, `continue: false` on the next tool call. Classification: rules first, then `claude -p`; a message is queued only when a rule matches (`pN:`, `later:`, "after that", "once that's done", "when you're done") or the model answers `queue` with confidence ≥ 0.9. Everything else passes through. A `shadow` setting logs decisions without acting. Store: one JSON file per session with a lock and atomic writes. Deliverable: install the plugin, type a task mid-run, watch it run after the current one, say "stop" and see the turn end.
- **M1 Make it trustworthy.** Implemented 2026-10-19 (plan `superpowers/plans/2026-10-19-m1-trustworthy.md`, record `m1-eval.md`, `m1-acceptance.md`): eval runner over the golden set with the asymmetric dependency score; the Decision contract v2 with the rationales as worked examples; `amend` with standard-library word similarity ranking the candidates and the model picking the target (`scripts/qsc/match.py`; the rapidfuzz-then-embeddings ladder of the original plan was not built, difflib and Jaccard were enough for the golden set) and `split` with the fake-split guard; the `parked` list and promotion; mixed-intent cases g09, g27, g46; Esc detection and the drop-with-notice policy; the `queue:` control channel and the `/queue` command; dedupe of held text resubmitted from the input box. Gate reached: 44 of 47, zero missed hard dependencies, both interrupt cases correct.
- **M2 Plan flow.** Just-in-time `plans/pN.md` from the previous task's handoff, the approval gate as the default with the `auto` override, `handoffs/pN.md`, re-planning a stale child, and done detection by verify command or the haiku judge. Implemented 2026-10-19 (§4.3, §4.4, §7; plan `superpowers/plans/2026-10-19-m2-plan-flow.md`), with the plan and handoff files under the plugin data dir rather than the repository.
- **M3 Hardening.** Implemented 2026-10-19 (plan `superpowers/plans/2026-10-19-m3-hardening.md`): idle-state markers and Esc during planning (§3, §6); per-directory session pointers and session garbage collection (§4.4); a spin guard that fires on an identical handback and a continuation budget of 6 (§4.3 D); an idempotent `auto` execution handover; the verify command's process group killed on timeout; control replies that name every accepted status, and shadow-mode tasks editable through the control channel; more harness-traffic patterns and the interrupt hook on `PreToolUse` only (D6); `hook_ms` latency percentiles and decision counts in the shadow report; the marketplace manifest, CI on Python 3.10, 3.12 and 3.14, an MIT licence and version 0.3.0. Live acceptance passed (scenarios T to W: idle markers, Esc during planning, two sessions, marketplace install in both forms; `m3-acceptance.md`).
- **M4 Daily-use polish.** Implemented 2026-10-19 (plan `superpowers/plans/2026-10-19-m4-daily-use.md`, record `m4-acceptance.md`, version 0.4.0): the pass-through notices (`nothing was queued, running now: …`, `Esc dropped p2, p3`, `running p2`, `p2 confirmed done`) carry a `systemMessage` so they appear on screen; `hook_ms` measured from the first Python statement of each entry script and the interpreter's own start-up printed by the report; stale session pointers pruned with the sessions; the interrupt script ignores anything but `PreToolUse`; README, CHANGELOG and manifests brought in line. Version 0.4.1 added the background comparison: in active mode with **Shadow: also ask the model** on, a rule-decided message also goes to the classifier in a detached `scripts/compare.py`, logged as a `shadow_compare` line.

## 11. Inference

- **Who runs the model.** Claude Code itself, through `claude -p`, on the user's own auth. No API key, no third-party service, no local server (decision D2b).
- **Which model.** The `haiku` alias for intake and for the done judge. If the M1 eval shows haiku missing the hard amend and split cases, raising the model is an M1 decision made with numbers.
- **What the model sees.** A fixed system prompt holding the class definitions and rules from §3 (written once, cached by the API), then one JSON payload: harness state, the running task, the queue with ids and intents, the last few turns from the transcript tail, and the message. The golden set's `rationale` fields are the prompt's worked examples.
- **What it returns.** JSON validated against the Decision schema via `--json-schema`: label, interrupt and parked flags, `depends_on`, amend `target`, split `parts`, confidence. The intake script re-validates it before acting.
- **Isolation.** `--setting-sources "" --strict-mcp-config --tools ""` plus the custom system prompt: no project context, no MCP servers, no tools, so the call cannot answer the message instead of classifying it or trigger hooks recursively.
- **When it is called.** Only when the rules in §4.1 cannot decide. Harness traffic, non-tasks, idle with an empty queue, and explicit markers never reach it. Measured 3.6 to 5.5 s wall-clock and about $0.0004 per call.
- **When it fails.** Timeout (15 s), parse error, schema mismatch or confidence below the threshold all mean `steer`: the message goes through as if the plugin were not installed, and the log marks the classifier field null so the miss is visible.
- **Call sites.** Intake on `UserPromptSubmit` for routing (M0), and the judge on `Stop` for done detection (M2). One wrapper (`scripts/qsc/model.py`), two prompts (`prompts/intake_system.md`, `prompts/judge_system.md`), two schemas (`classify.SCHEMA`, `done.JUDGE_SCHEMA`). The judge fails open as `done`, the classifier as `steer`. The judge is not called for a turn the user typed (decision D8), and the verify command or judge runs before the Stop hook takes the store lock.

## 12. Libraries

The code uses the standard library only: `fcntl`, `tempfile` and `os.replace` for the store, `difflib` for amend matching, `subprocess` for `claude -p` and the verify command, `argparse` for the CLI and the eval runner. [libraries.md](libraries.md) records what was researched on 2026-10-18 and why none of the candidate packages was needed, and keeps the mod route as the upgrade path.

## 13. Reference projects

- **Jev_steer_or_queue**: router, ~300 lines. Source of the hook plumbing, `prompt_id` detection, interrupt path, shadow mode and `docs/test-results.md`.
- **claude-task-manager**: queue, ~1,200 lines, `pending_tasks.py`. Source of the transcript-tail reader, detached Haiku judge, harness-traffic filter and store-hygiene lessons.

## 14. What was lost

The original wording of the design beyond what the plan and the golden set pin down. Nothing in the lost session is needed to build on this spec: every behaviour above is described from the code, and the log record is `{ts, event, hook_ms, ...}` as written by `scripts/qsc/log.py` (event names: `intake`, `dispatch`, `control`, `interrupt`, `shadow_compare`, `shadow_verdict`, `gc`, `handoff`, `handoff_write_failed`, `plan_failed`, `replan`, `store_corrupt`).
