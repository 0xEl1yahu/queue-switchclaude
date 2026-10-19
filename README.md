# queue-switchclaude

A harness for Claude Code that sorts messages typed mid-run into **steer / queue / amend / split** (with `interrupt` and `parked` flags), runs queued work in dependency order, and plans each task just in time from the previous task's handoff.

Today Claude Code injects any mid-run message into the running turn. "When you're done, list the files" runs *now*, in parallel with what it was doing. This project puts three hooks around that: intake classifies the message, a tool-call hook can hard-stop the turn, and a Stop hook dispatches the next queued task.

## Status

M2 plan flow implemented (2026-10-19): every queued task is planned just in time by the session's own Claude from the previous task's handoff, shown for approval before it runs (`lgtm`, or `auto` to skip the gate), and judged done by its verify command or a haiku judge instead of "Claude stopped". M0 (steer / queue / interrupt / dispatch, rules-first classifier, shadow switch) and M1 (golden-set eval, amend and split, parked, Esc handling, the `queue:` control channel) are live-accepted: [`docs/m0-acceptance.md`](docs/m0-acceptance.md), [`docs/m1-acceptance.md`](docs/m1-acceptance.md). M2 live acceptance is the next step. The five open decisions were made on 2026-10-18: build fresh, classic hooks in Python packaged as a plugin, classifier through `claude -p` with rules first, approve each plan, verify-or-judge for done, Esc drops the queue with a notice. See [`docs/decisions.md`](docs/decisions.md).

## What a session looks like

Type a task. While Claude works on it, type `later: add a goodbye.py that prints goodbye`: the message is blocked, stored as `p2` after `p1`, and the notification says so. When Claude stops, the Stop hook checks `p1` is really done (its verify command if it has one, else a haiku judge over Claude's closing message; a "not finished" verdict is handed back at most twice), writes `handoffs/p1.md`, and hands `p2` over for planning: Claude reads that handoff, writes `plans/p2.md`, presents it in a few lines and stops. Type `lgtm` (or `ok`, `go`, `ship it`, or `queue: approve`) and the plan runs; type `looks good but print the date first` and the plan is revised and shown again; type something unrelated and it runs as a normal turn, after which the plan is shown again. If Claude stops by asking a question, the next message is the answer and the task resumes; `ok, merge` instead confirms the task done.

## The `queue:` control channel

A message starting with `queue:` (or `/queue`) runs one command against the queue and is answered in place, in every state, without a model call and without starting a turn:

| Command | Effect |
|---|---|
| `queue: list` | One line: the running, queued, parked, paused and planned tasks; the plan waiting on you reads `planned (awaiting approval)` |
| `queue: show pN` | `pN <status>: <text>`, plus the plan and handoff paths that exist, the verify command and `auto` when set |
| `queue: cancel pN` | A queued, parked, paused or planned task becomes `cancelled` |
| `queue: promote pN` / `queue: park pN` | Parked to queued, or queued to parked |
| `queue: pause` / `queue: resume` | Stop dispatching anything, or start again; `queue: resume pN` returns a paused task to the queue |
| `queue: drop` | Every queued and parked task becomes `dropped` |
| `queue: verify pN <command>` | The rest of the line, verbatim, decides done for `pN`: exit 0 on Stop means done, anything else is handed back with the output tail (120 s timeout) |
| `queue: auto pN` / `queue: manual pN` | Skip the approval gate for `pN` (its plan flows straight into execution), or wait for approval again |
| `queue: approve` | Run the plan that is waiting for approval; same as typing `lgtm` |
| `queue: help` | The list above |

The bare form `queue list` (no colon) works for `list`, `cancel`, `promote`, `park`, `drop`, `pause`, `resume` and `help`; the M2 commands need the colon or the slash.

- [`docs/design-spec.md`](docs/design-spec.md): the design, reconstructed on 2026-10-18 after the original session was lost.
- [`docs/flows.md`](docs/flows.md): diagrams of the user flow, the intake decision logic, the task lifecycle, dispatch and interrupt.
- [`docs/decisions.md`](docs/decisions.md): D1 to D5, with the evidence and the rejected options.
- [`docs/libraries.md`](docs/libraries.md): Python libraries per problem area (store, DAG, classifier, matching, eval), with sketches.
- [`queue-harness-plan.md`](queue-harness-plan.md): the surviving side plan. Answers to the open questions, lessons borrowed from two prior projects, risks, and the M0–M3 milestones.
- [`eval/golden_queue_intake.jsonl`](eval/golden_queue_intake.jsonl): 47 labelled intake cases across five scenarios, with rationales and acceptable alternatives. The schema is in the spec, section 8.

## Install

From inside Claude Code, add the repository as a marketplace and install the plugin from it:

```bash
/plugin marketplace add 0xEl1yahu/queue-switchclaude
/plugin install queue-switchclaude@queue-switchclaude
```

Restart the session afterwards so the hooks load. To remove it: `/plugin uninstall queue-switchclaude@queue-switchclaude`.

## Install for development

Run a checkout directly, without installing it, to pick up code changes on every hook call:

```bash
git clone https://github.com/0xEl1yahu/queue-switchclaude.git
claude plugin validate queue-switchclaude
claude --plugin-dir queue-switchclaude
```

Toggle **Shadow mode** under `/config` to log every decision without acting on it. Everything the plugin writes is under `$CLAUDE_PLUGIN_DATA` (fallback when the variable is unset: `~/.claude/queue-switchclaude/`): the per-session store at `sessions/<session_id>.json`, the plans Claude writes at `sessions/<session_id>/plans/pN.md`, the handoffs dispatch files at `sessions/<session_id>/handoffs/pN.md`, and the decision log at `log/<session_id>.jsonl`. Nothing is written into the repository Claude is working in.

Tests: `python3 -m pytest -q` (stdlib-only code under `scripts/` and `eval/`; the fake `claude` in `tests/fakes/` stands in for the model). Golden-set eval: `python3 eval/run_golden.py --rules-only` and `python3 eval/run_golden.py --report eval/reports/<name>.md`.

## Next

Working product first (spec §10):

1. **M0 vertical slice** (done): steer / queue / interrupt end to end in one session, chain dependencies, dispatch on Stop, rules-first classification via `claude -p`, a shadow switch.
2. **M1 trustworthy** (done): golden-set eval, amend and split, parked, Esc handling, `/queue` command.
3. **M2 plan flow** (implemented, live acceptance next): just-in-time plans, approval gate with `auto`, handoffs, verify-or-judge, re-planning a stale child.
4. **M3 hardening**: multi-session store hygiene, harness-traffic edge cases, the Stop-hook continuation cap, whatever the usage logs show.
