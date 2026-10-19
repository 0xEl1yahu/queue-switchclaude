# Decisions

Eight decisions that shape the plugin. Each entry: the decision, the options rejected, the evidence. Do not reverse one without reading its entry; each records a measured reason, and D5 records the user's explicit choice over the author's recommendation. D1 to D5 were decided 2026-10-18 in conversation, before the first line of code; D6 to D8 on 2026-10-19 from the live acceptance runs.

## D1. Base: build fresh

Own code, no fork. Jev_steer_or_queue's hook tricks are now in the official hooks reference (`prompt_id`, `continue: false`, Stop `additionalContext`, `last_assistant_message`), its queue is a FIFO and its classifier is coupled to TypeSafe, so most of its 300 lines would be replaced. claude-task-manager's non-blocking philosophy and word-overlap matching are what the plan found wanting.

## D2. Runtime: classic hooks, all Python, packaged as a plugin

The harness is a Claude Code plugin whose `hooks/hooks.json` declares `command` hooks on `UserPromptSubmit`, `Stop` and `PreToolUse`/`PostToolUse`, each running a Python script under `${CLAUDE_PLUGIN_ROOT}`. (D6 later dropped the `PostToolUse` entry.)

Rejected: a mod (in-process JavaScript hooks module). It would have given instant interrupt (`$.turn.abort`), a clean dispatch turn (`$.prompt.submit`), a live pane, `turn.complete` with Esc detection, and a native mid-turn flag (`turnId` on `prompt.submit`). It was rejected because the core would have to be JavaScript and the mod surface is marked early access. If the classic-hook limits bite (interrupt only at tool boundaries, no Esc signal, dispatch via `additionalContext`), the mod route is the documented upgrade path; the reference is in [libraries.md](libraries.md) and the type declarations at `mods/types/claude-code.d.ts` in the claude-code repo.

Also rejected: a `prompt`-type hook as the classifier. On `UserPromptSubmit` it can only return `{ok, reason}`, and `ok: false` ends the turn with a warning line. No structured output, no store access. It is a gate, not a router.

## D2b. Classifier caller: `claude -p`, rules first

The intake script shells out to:

```
claude -p --model haiku --setting-sources "" --strict-mcp-config --tools "" \
  --output-format json --json-schema <Decision schema> --system-prompt <classifier prompt> <payload>
```

Uses the user's Claude Code auth. No API key, no third party.

Measured on this machine, 2026-10-18, Claude Code 2.1.296:

| Call | Model time | Wall-clock | Cost |
|---|---|---|---|
| one-word label, no schema, run 1 | 3.8 s | 5.5 s | $0.0002 |
| one-word label, no schema, run 2 | 3.2 s | 4.8 s | $0.0002 |
| golden case g05 with the JSON schema | 2.1 s | 3.6 s | $0.0004 |

g05 came back `{"label":"queue","depends_on":["p1"],"interrupt":false,"parked":false,"confidence":0.9}`, which is the expected answer.

That is 3 to 5 s per classified message, against the plan's 1 to 2 s hope, so the model is called only when rules cannot decide:

1. Harness traffic and non-tasks pass through (plan §3).
2. State `idle` with an empty queue: everything actionable is a new task, pass through and record.
3. Explicit markers decide without the model: "stop" alone or "stop that now" (interrupt), `pN:` prefix, `later:` prefix (queue), "maybe later" (parked), a numbered list (split), a lone `.` (accept).
4. Everything else goes to the model.

Rejected: Anthropic API key direct (sub-second, but a key on every machine and separate billing); Ollama on the lab PC (latency and quality unproven, server must be reachable).

Measured since: the rules alone decide 8 of the 47 golden cases (`eval/run_golden.py --rules-only`), and in the live acceptance runs a rule-decided message costs about 90 ms of hook time against 3.4 to 4.7 s for a model-decided one. `claude -p` start-up cannot be trimmed from a hook (no warm daemon is available), which is why the rules exist and why 0.4.1 runs the comparison call detached.

## D3. Dispatch default: approve each plan

When a task becomes ready, the Stop hook asks the session's Claude to write `plans/pN.md` and present it, and ends the turn with state `awaiting_approval` (the harness never writes a plan itself). "lgtm" runs it; "looks good but do X first" revises it without approving (golden cases g44 to g47). A task opts out of the gate with `queue: auto pN`; the per-message opt-out ("run it without asking") in the original decision was never built.

Rejected: auto-run by default (errors in dependency inference compound before the user sees them; the Stop-hook continuation cap of 8 only bounds runaway chains, it does not prevent wrong work). Rejected: size-based auto-run (an extra judgement the golden set does not cover).

## D4. Done detection: verify command if set, else judge

A task may carry a verify command; if it exits 0 on Stop, the task is done. Otherwise the Stop hook runs a haiku judge through `claude -p` over `last_assistant_message` (and the plan), answering done / not done / asked a question. The user's next message can also confirm: "ok, merge" marks the previous task done. The judge runs inline: `hooks/hooks.json` gives the Stop hook 180 s (Claude Code's default would be 600 s) and the call takes about 4 s, so no detached process.

Rejected: judge only (a passing closing message is not a passing test). Rejected: verify command only (every untested task would wait on the user).

## D5. Esc: drop with a notice

When the next message arrives with a new `prompt_id` while state still says `running` and no Stop fired for that task, the harness treats the turn as interrupted: the interrupted task is marked `interrupted`, every `queued` (and `shadow_queued`) task is dropped, and a notice is attached to the next prompt as `additionalContext` listing what was dropped (ids and intents) so the user can re-queue by pasting. Assumption, not yet confirmed: `parked` items survive, because they never dispatch on their own; `done` history survives.

Rejected (my recommendation): pause and keep everything. Rejected: ask on the next message. The user chose drop for simplicity and predictability: Esc means "stop the plan".

The assumption held: `intake._new_turn` drops only `queued` tasks, so `parked` and `done` survive (M1). M3 narrowed the rule by state: a new `prompt_id` while the store says `planning` means Esc cut a planning turn, not work, so nothing is dropped; the task being planned returns to `queued`, its partial plan file is deleted, and the notice reads `the planning turn for pN was interrupted; it is queued again` (spec §6).

---

Decided 2026-10-19, during the M2 and M3 work. The evidence is in the live runs: `m2-acceptance.md`, `m2-fixes-acceptance.md`, and `m0-acceptance.md` for the interrupt path.

## D6. Interrupt hook on `PreToolUse` only

`hooks/hooks.json` registers `scripts/interrupt.py` on `PreToolUse` alone; the `PostToolUse` entry of D2 is gone. Every tool call costs one Python launch for the interrupt check instead of two. Nothing is lost: a `PostToolUse` boundary is always followed either by the next `PreToolUse`, where the flag is honoured, or by `Stop`, where dispatch honours it (`reset_after_interrupt`, nothing dispatched, the same drop as D5). Since 0.4.0 `interrupt.handle` returns before reading the store for any event but `PreToolUse`, and likewise when `tool_name` is `AskUserQuestion`: that turn is already handing control to the user, and ending it there would only swallow the question.

Evidence: in every live run the interrupt fired on `PreToolUse` (`m0-acceptance.md`, scenario B: `PreToolUse:Bash hook stopped continuation: queue-switchclaude: stopped as requested`, log `via: PreToolUse`, the turn halted 1.92 s after the send, with echo 6 already running). A hook launch costs 60 to 120 ms on this machine (the rules-only intake delays recorded in `m1-acceptance.md` and `m2-acceptance.md`), and the interrupt check paid it twice per tool call whether or not a stop was pending; the store is read without the lock, so the launch itself is most of the cost.

Rejected: keeping both hooks (double the per-tool-call cost for a boundary that never fired first). Rejected: the mod route's `$.turn.abort` (D2 stands; instant abort would need the JavaScript core).

## D7. Plans and handoffs live under the plugin data dir

`paths.plan_path(session, task)` is `<data dir>/sessions/<session>/plans/<task>.md` and `paths.handoff_path` is `.../handoffs/<task>.md`, next to the session's `<session>.json` store. The original design (D3, spec §4.3) said `plans/pN.md` in the user's repository. Every handover names the absolute path, Claude reads and writes the files there, and `queue: show pN` prints the ones that exist. The data dir is `$CLAUDE_PLUGIN_DATA`, else the newest `~/.claude/plugins/data/queue-switchclaude*` directory, else `~/.claude/queue-switchclaude`.

Evidence: `m2-acceptance.md`, scenario K: "The user's working tree held only `hello.py`; the plan and handoff are under the data dir", with the files at `…/sessions/d9d02c35-…/plans/p2.md` and `.../handoffs/p1.md` and Claude reading `handoffs/p1.md` before writing the plan. The cost surfaced in `m2-fixes-acceptance.md`, S2: a command run by the Bash tool has no `CLAUDE_PLUGIN_DATA`, so `queue_cli.py` answered `no session found` until `commands/queue.md` passed the variable through and `paths.data_dir()` learnt the installed-plugin fallback; the re-run logged `control: approve via queue_cli` against the right session.

Rejected: files in the working tree (they show in `git status` and in the diff side panel, get committed by accident, and in the scratch repo without commits the panel sat over the notification area, `m1-acceptance.md` issue 3). Rejected: a temporary directory per turn (the next task's planning turn reads the previous task's handoff, so the files must outlive the Stop hook that wrote them).

## D8. Prompt turns are not judged

Every turn the user types is registered as a task with `Task.kind = "prompt"`, so queued work can chain after it (`intake._chain_deps`) and D5's Esc detection has a running task to mark. On Stop such a task closes as `stopped` with reason `prompt turn ended` and no model call, unless the user gave it a verify command (`dispatch._verdict_for`). Only work the harness queued (`kind = "task"`) goes through D4's verify-or-judge decision. A handoff file is still written for a prompt turn, so a queued task that depends on it can read what the user's own turn produced.

Evidence: `m2-acceptance.md`, issue 4 (scenario O): a plain question typed while a plan waited cost three model calls (the classifier to route it, the judge to close it as task `p5` with its own `handoffs/p5.md`, and the re-present continuation). After the change, `m2-fixes-acceptance.md`, scenario P: "No judge call: the turn closed with `verdict: stopped, reason: prompt turn ended` 2.2 s after the send (the model's own answer time), store `p1 done` with `kind: prompt`", and every Stop in P to S took under a second except where a judge or a verify command ran for a queued task.

Rejected: judging every turn (about 4 s per Stop, a store lock held for that long while the next message waits, and a handoff written for a question that produced nothing). Rejected: not registering prompt turns at all (the chain rule and Esc detection would have no running task to work from).
