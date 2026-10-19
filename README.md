# queue-switchclaude

A harness for Claude Code that sorts messages typed mid-run into **steer / queue / amend / split** (with `interrupt` and `parked` flags), runs queued work in dependency order, and plans each task just in time from the previous task's handoff.

Today Claude Code injects any mid-run message into the running turn. "When you're done, list the files" runs *now*, in parallel with what it was doing. This project puts three hooks around that: intake classifies the message, a tool-call hook can hard-stop the turn, and a Stop hook dispatches the next queued task.

## Status

M3 hardening implemented (2026-10-19, version 0.3.0): the plugin installs from GitHub through the plugin marketplace; two sessions in different folders keep separate queues (a session pointer per working directory) and sessions nobody has touched for 30 days are garbage-collected; `later:`, `maybe later` and `stop` do the sensible thing at idle (run now, park, no-op); Esc during a planning turn puts the task back in the queue instead of losing it; a spin guard stops an instruction that comes back twice, and the harness pauses its own chain after six continuations; harness-generated prompts (task notifications, skill invocations, interrupted-request markers) never become tasks; a verify command that times out is killed with its whole process group; the interrupt hook runs on `PreToolUse` only; and the shadow report prints hook latency percentiles. M2 plan flow (just-in-time plans, the approval gate with `auto`, handoffs, verify-or-judge, re-planning a stale child) and its review follow-ups are live-accepted: [`docs/m2-acceptance.md`](docs/m2-acceptance.md), [`docs/m2-fixes-acceptance.md`](docs/m2-fixes-acceptance.md), after M0 and M1 ([`docs/m0-acceptance.md`](docs/m0-acceptance.md), [`docs/m1-acceptance.md`](docs/m1-acceptance.md)). M3 live acceptance (idle markers, Esc during planning, two sessions, the marketplace install) is the next step. Decisions D1 to D8 (build fresh, classic hooks in Python packaged as a plugin, classifier through `claude -p` with rules first, approve each plan, verify-or-judge for done, Esc drops the queue with a notice, interrupt on `PreToolUse` only, plans and handoffs under the data dir, prompt turns not judged) are in [`docs/decisions.md`](docs/decisions.md).

## What a session looks like

Type a task. While Claude works on it, type `later: add a goodbye.py that prints goodbye`: the message is blocked, stored as `p2` after `p1`, and the notification says so. When Claude stops, the Stop hook closes `p1` (a turn you typed is simply `stopped`; a queued task is checked by its verify command if it has one, else by a haiku judge over Claude's closing message, and a "not finished" verdict is handed back at most twice), writes `handoffs/p1.md`, and hands `p2` over for planning: Claude reads that handoff, writes `plans/p2.md`, presents it in a few lines and stops. Type `lgtm` (or `ok`, `go`, `continue`, `ship it`, or `queue: approve`) and the plan runs; type `looks good but print the date first` and the plan is revised and shown again; type something unrelated and it runs as a normal turn, after which the plan is shown again. If Claude stops by asking a question, the next message is the answer and the task resumes; `ok, merge` instead confirms the task done. A task whose verify command fails twice is set aside as `failed_verify` and you are told; `queue: promote p2` retries it.

When nothing is running, the markers mean what you would expect: `later: write i.txt` writes it now (the marker is stripped and the context line says so), `maybe later we can add a README` parks `p2` and nothing runs, and `stop` answers `nothing is running`. Pressing Esc while Claude writes a plan puts that task back in the queue, with its half-written plan deleted and nothing else dropped; whatever you type next runs as a normal turn and the task is planned again after it. Pressing Esc while a task is executing drops the queue with a notice listing what was dropped (parked and done items survive).

## The `queue:` control channel

A message starting with `queue:` runs one command against the queue and is answered in place, in every state, without a model call and without starting a turn. The slash forms `/queue <command>` and `/queue-switchclaude:queue <command>` are caught by the same rule before the slash command's own CLI runs; the bare form `queue <command>` (no colon) works when the first word is a known command.

| Command | Effect |
|---|---|
| `queue: list` | One line: the running, queued, parked, paused, planned and failed-verify tasks; the plan waiting on you reads `planned (awaiting approval)`; a paused queue says so |
| `queue: show pN` | `pN <status>: <text>`, plus the plan and handoff paths that exist, the verify command and `auto` when set |
| `queue: cancel pN` | A queued, parked, paused, planned or failed-verify task becomes `cancelled` |
| `queue: promote pN` | A parked, paused or failed-verify task returns to the queue |
| `queue: park pN` | A queued task becomes `parked` |
| `queue: pause` / `queue: resume` | Stop dispatching anything, or start again |
| `queue: resume pN` | A parked, paused or failed-verify task returns to the queue (same as `promote`) |
| `queue: drop` | Every queued and parked task becomes `dropped` |
| `queue: verify pN <command>` | The rest of the line, verbatim, decides done for `pN`: exit 0 on Stop means done, anything else is handed back with the output tail (120 s timeout; the command's process group is killed if it runs over) |
| `queue: auto pN` / `queue: manual pN` | Skip the approval gate for `pN` (its plan flows straight into execution), or wait for approval again |
| `queue: approve` | Run the plan that is waiting for approval; same as typing `lgtm` |
| `queue: help` | The list above; so does any unknown word |

Task ids can be typed as `p2`, `P2` or `2`. In shadow mode `cancel`, `park` and `drop` also act on the tasks shadow mode recorded. A failed command names every status it accepts (`p2 is not parked, paused or failed_verify`).

- [`docs/design-spec.md`](docs/design-spec.md): the design, reconstructed on 2026-10-18 after the original session was lost, with the state machines as built.
- [`docs/flows.md`](docs/flows.md): diagrams of the user flow, the intake decision logic, the task lifecycle, dispatch and interrupt.
- [`docs/decisions.md`](docs/decisions.md): D1 to D8, with the evidence and the rejected options.
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

Toggle **Shadow mode** under `/config` to log every decision without acting on it (**Shadow: also ask the model** logs the classifier's answer next to the rule's). Everything the plugin writes is under `$CLAUDE_PLUGIN_DATA` (fallback when the variable is unset: the newest `~/.claude/plugins/data/queue-switchclaude*` directory, then `~/.claude/queue-switchclaude/`): the per-session store at `sessions/<session_id>.json`, the plans Claude writes at `sessions/<session_id>/plans/pN.md`, the handoffs dispatch files at `sessions/<session_id>/handoffs/pN.md`, the decision log at `log/<session_id>.jsonl`, and the session pointers `last_session` and `pointers/<hash of cwd>` that the `/queue` slash command's CLI uses to find the right session. Nothing is written into the repository Claude is working in. The first Stop of each session deletes the files of sessions untouched for 30 days (never its own).

Tests: `python3 -m pytest -q` (stdlib-only code under `scripts/` and `eval/`; the fake `claude` in `tests/fakes/` stands in for the model). CI runs the suite on Python 3.10, 3.12 and 3.14 (`.github/workflows/test.yml`). Golden-set eval: `python3 eval/run_golden.py --rules-only` and `python3 eval/run_golden.py --report eval/reports/<name>.md`. Usage report from the logs: `python3 eval/shadow_report.py [--since 7d]` (rule versus model decisions, classifier latency, hook latency p50 and p95, a count per intake decision).

## Next

Working product first (spec §10):

1. **M0 vertical slice** (done): steer / queue / interrupt end to end in one session, chain dependencies, dispatch on Stop, rules-first classification via `claude -p`, a shadow switch.
2. **M1 trustworthy** (done): golden-set eval, amend and split, parked, Esc handling, `/queue` command.
3. **M2 plan flow** (done, live-accepted): just-in-time plans, approval gate with `auto`, handoffs, verify-or-judge, re-planning a stale child.
4. **M3 hardening** (implemented, live acceptance next): idle markers, Esc during planning, per-directory session pointers and garbage collection, spin guard and continuation budget, harness-traffic patterns, interrupt on `PreToolUse` only, hook latency in the report, marketplace install and CI.
