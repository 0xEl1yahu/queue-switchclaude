# queue-switchclaude

A harness for Claude Code that sorts messages typed mid-run into **steer / queue / amend / split** (with `interrupt` and `parked` flags), runs queued work in dependency order, and plans each task just in time from the previous task's handoff.

Today Claude Code injects any mid-run message into the running turn. "When you're done, list the files" runs *now*, in parallel with what it was doing. This project puts three hooks around that: intake classifies the message, a tool-call hook can hard-stop the turn, and a Stop hook dispatches the next queued task.

## Status

M4 daily-use polish (2026-10-19, version 0.4.0): the notices that used to reach only the model now also appear on screen (`nothing was queued, running now: …`, `Esc dropped p2, p3`, `running p2`, `p2 confirmed done`); hook latency is measured from the first Python statement and the report prints the interpreter start-up it cannot include; stale session pointers are pruned with the sessions; the interrupt script ignores anything but `PreToolUse`; the help line names every control word; and this README, a [`CHANGELOG.md`](CHANGELOG.md) and the manifest descriptions match the code. The live re-run of the last fixes (paused queue, `stop` and `later:` after Esc, the notices) is the next step. M3 hardening is live-accepted in both marketplace forms ([`docs/m3-acceptance.md`](docs/m3-acceptance.md)), after M2 ([`docs/m2-acceptance.md`](docs/m2-acceptance.md), [`docs/m2-fixes-acceptance.md`](docs/m2-fixes-acceptance.md)), M1 ([`docs/m1-acceptance.md`](docs/m1-acceptance.md)) and M0 ([`docs/m0-acceptance.md`](docs/m0-acceptance.md)). Decisions D1 to D8 (build fresh, classic hooks in Python packaged as a plugin, classifier through `claude -p` with rules first, approve each plan, verify-or-judge for done, Esc drops the queue with a notice, interrupt on `PreToolUse` only, plans and handoffs under the data dir, prompt turns not judged) are in [`docs/decisions.md`](docs/decisions.md).

## What a session looks like

Type a task. While Claude works on it, type `later: add a goodbye.py that prints goodbye`: the message is blocked, stored as `p2` after `p1`, and the notification says so. When Claude stops, the Stop hook closes `p1` (a turn you typed is simply `stopped`; a queued task is checked by its verify command if it has one, else by a haiku judge over Claude's closing message, and a "not finished" verdict is handed back at most twice), writes `handoffs/p1.md`, and hands `p2` over for planning: Claude reads that handoff, writes `plans/p2.md`, presents it in a few lines and stops. Type `lgtm` (or `ok`, `go`, `continue`, `ship it`, or `queue: approve`) and the plan runs, with `running p2` on screen; type `looks good but print the date first` and the plan is revised and shown again; type something unrelated and it runs as a normal turn, after which the plan is shown again. If Claude stops by asking a question, the next message is the answer and the task resumes; `ok, merge` instead confirms the task done. A task whose verify command fails twice is set aside as `failed_verify` and you are told; `queue: promote p2` retries it.

When nothing is running, the markers mean what you would expect: `later: write i.txt` writes it now (the marker is stripped and the screen says `nothing was queued, running now: write i.txt`), `maybe later we can add a README` parks `p2` and nothing runs, and `stop` answers `nothing is running`. Pressing Esc while Claude writes a plan puts that task back in the queue, with its half-written plan deleted and nothing else dropped; whatever you type next runs as a normal turn and the task is planned again after it. Pressing Esc while a task is executing drops the queue with a notice listing what was dropped (parked and done items survive).

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
- [`CHANGELOG.md`](CHANGELOG.md): what each milestone shipped, newest first.

## Requirements

- macOS or Linux. The per-session store is guarded by an `fcntl` lock, so Windows is out (WSL is fine).
- Python 3.10 or later on `PATH` as `python3`. The hooks run `python3 <script>`; nothing is installed and no package is needed beyond the standard library.
- Claude Code 2.1.302 or later, the version every acceptance run used. The classifier and the judge call the same `claude` binary with `-p`, so the plugin spends model calls from your own account: about one call per message the rules cannot decide, and one per queued task closed without a verify command.

## Install

From inside Claude Code, add the repository as a marketplace and install the plugin from it:

```bash
/plugin marketplace add 0xEl1yahu/queue-switchclaude
/plugin install queue-switchclaude@queue-switchclaude
```

The first line answers `Successfully added marketplace: queue-switchclaude` and reloads the plugins. The second opens the Plugins panel on the plugin's details page (the description, the trust warning) and walks through two screens before anything is active, so do not take the details page for a hang:

1. The scope picker: `Install for you (user scope)` is the default and the one to take; project scope writes the install into the repository's shared settings, local scope keeps it to you in this repository only.
2. The plugin's options screen, `Configure queue-switchclaude`: **Shadow mode** and **Shadow: also ask the model**, both `false`. Leave them for now (see "Daily use" for when to turn them on) and choose `Save configuration`.

Then `✓ Installed and configured queue-switchclaude. Plugin is now active.` Restart the session so the hooks load (`/exit`, then `claude` again); the first prompt of the new session creates the plugin's data directory. Both options can be changed later under `/config` without reinstalling.

To remove it:

```bash
/plugin uninstall queue-switchclaude@queue-switchclaude
```

The panel asks whether to delete the plugin's persistent data (its queues, plans, handoffs and logs; `y` deletes, `n` keeps them for a reinstall). The panel then stays open on the Installed tab: press Escape to get back to the prompt, because a slash command typed into the panel does not run. The marketplace entry is removed separately, and the reliable way is from a shell rather than the panel:

```bash
claude plugin marketplace remove queue-switchclaude
```

After both steps `~/.claude/settings.json` has no `enabledPlugins`, `pluginConfigs` or `extraKnownMarketplaces` entry for the plugin, and nothing needs editing by hand.

## Install for development

Run a checkout directly, without installing it, to pick up code changes on every hook call:

```bash
git clone https://github.com/0xEl1yahu/queue-switchclaude.git
claude plugin validate queue-switchclaude
claude --plugin-dir queue-switchclaude
```

Everything the plugin writes is under `$CLAUDE_PLUGIN_DATA` (fallback when the variable is unset: the newest `~/.claude/plugins/data/queue-switchclaude*` directory, then `~/.claude/queue-switchclaude/`): the per-session store at `sessions/<session_id>.json`, the plans Claude writes at `sessions/<session_id>/plans/pN.md`, the handoffs dispatch files at `sessions/<session_id>/handoffs/pN.md`, the decision log at `log/<session_id>.jsonl`, and the session pointers `last_session` and `pointers/<hash of cwd>` that the `/queue` slash command's CLI uses to find the right session. Nothing is written into the repository Claude is working in. The first Stop of each session deletes the files of sessions untouched for 30 days (never its own) and the pointers that no longer name a session.

Tests: `python3 -m pytest -q` (stdlib-only code under `scripts/` and `eval/`; the fake `claude` in `tests/fakes/` stands in for the model). CI runs the suite on Python 3.10, 3.12 and 3.14 (`.github/workflows/test.yml`). Golden-set eval: `python3 eval/run_golden.py --rules-only` and `python3 eval/run_golden.py --report eval/reports/<name>.md`; the model answers are cached under `eval/cache/`, so a fresh clone reproduces the 44 of 47 gate offline.

## Daily use

The plugin is built to be judged on real sessions, not on the golden set. The way to do that is one week of watching, then a report.

1. Under `/config`, turn on **Shadow mode** and **Shadow: also ask the model** (both take effect on the next prompt; no restart). In shadow mode the plugin blocks nothing and dispatches nothing: Claude Code's stock behaviour runs, and every message is logged with what the harness *would* have done. The second switch makes intake also ask the classifier about the messages the rules decided by themselves, so each of those log lines holds the rule's answer and the model's answer side by side. It costs one `claude -p` call (3 to 5 s, in the background of a turn that runs anyway) per mid-run message.
2. Work as usual for a week. Type `later: …`, `stop`, `maybe later …` and `queue: list` when you mean them; nothing happens, but the decision is recorded. In shadow mode `queue: cancel`, `park` and `drop` still act on the tasks it recorded.
3. Run the report:

```bash
python3 eval/shadow_report.py --since 7d
```

What the lines mean:

- `shadow report: N intake decision(s) in F log file(s)`: how many mid-run messages were sorted, over how many sessions.
- `rule-decided` / `model-decided`: how many the rule table settled alone (fast, free) and how many went to the classifier.
- `compared` and `disagreements`: of the rule-decided messages that were also sent to the model, how many times the model would have sorted the message differently. The `recent disagreements` lines that follow show the message text with both labels; a disagreement is a candidate for a new rule, a rule to loosen, or a golden-set case.
- `mean classifier latency`: the average `claude -p` round trip, the price of every message the rules cannot decide.
- `hook latency`: p50 and p95 of `hook_ms` for the intake and Stop hooks, with and without a model call, measured from the first Python statement of the hook to its log line; `python3 start-up` is the interpreter's own cost (the median of five `python3 -c pass` runs on this machine), which comes on top of `hook_ms` and which the plugin cannot reduce.
- `decisions`: a count per intake decision (`new_turn`, `control`, `dedupe`, `approve`, `parked_idle`, `noop`, …), which shows what the harness is actually used for.

The log the report reads is `log/<session_id>.jsonl` under the data directory above, one JSON object per line; the `message` field holds the text, so delete the files for sessions you would rather not keep. When the report reads well, turn **Shadow mode** off (keep or drop the second switch; it does nothing outside shadow mode) and the harness starts acting on its decisions.

## Known limits

- A blocked message (`later: …`, `stop`, any `queue:` command) returns to the input box after the notification. Clear it with Ctrl-U before typing the next message, or the two texts concatenate into one prompt; if you press Enter on it by accident, dedupe blocks the resend with `that is already pN`.
- A message the rules cannot decide costs a classifier call of about 4 s (3.4 to 4.7 s in the acceptance runs). A steer typed early in a short turn can therefore land after the work it meant to shape; it is still delivered and acted on.
- `stop` takes effect at the next tool call: the intake hook sets the flag and the `PreToolUse` hook ends the turn (about 2 s in the acceptance runs). A turn that is only writing text, with no tool call left, cannot be halted this way; use Esc.
- A message typed in the seconds after Claude's closing message can wait a few seconds for the Stop hook's judge call before it is sorted; the status line shows `sorting your message…` while it waits.
- The mid-run notification shows one line, so `queue: list` and `queue: help` are written as one line; at idle a reply is a persistent `blocked by hook` line in the transcript instead.
- An unrelated question typed while a plan waits for approval costs a classifier call to route it and a short continuation to show the plan again afterwards; the plan is never lost, but the question becomes a task record of its own (`kind: prompt`, closed as `stopped`, no judge).

## Next

Working product first (spec §10):

1. **M0 vertical slice** (done): steer / queue / interrupt end to end in one session, chain dependencies, dispatch on Stop, rules-first classification via `claude -p`, a shadow switch.
2. **M1 trustworthy** (done): golden-set eval, amend and split, parked, Esc handling, `/queue` command.
3. **M2 plan flow** (done, live-accepted): just-in-time plans, approval gate with `auto`, handoffs, verify-or-judge, re-planning a stale child.
4. **M3 hardening** (done, live-accepted): idle markers, Esc during planning, per-directory session pointers and garbage collection, spin guard and continuation budget, harness-traffic patterns, interrupt on `PreToolUse` only, hook latency in the report, marketplace install and CI.
5. **M4 daily-use polish** (implemented, live re-run next): visible notices, honest hook latency, stale pointer cleanup, README and CHANGELOG; then a week of shadow compare in real sessions decides what comes after.
