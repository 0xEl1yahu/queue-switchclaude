# M1 live acceptance

Run on 2026-10-19 (10:08 to 10:17 UTC, 11:08 to 11:17 BST) in Claude Code 2.1.302 with Opus 5.5, auto permission mode, plugin loaded with `--plugin-dir` from the main checkout at commit `88a2b68` (data dir `~/.claude/plugins/data/queue-switchclaude-inline/`). Same method as `docs/m0-acceptance.md`: the session ran in tmux (150x45) in `/tmp/qsc-demo`, a scratch `git init` repo with no files. Messages were typed with `tmux send-keys -l` and sent with `Enter`; each send was timestamped in UTC (perl `Time::HiRes`, taken just before the `Enter`) so hook delay could be read against the `ts` of the matching decision-log line. Mid-run messages were sent 2.0 s after the first prompt of the scenario; the pane was captured 1.5 s after each send and again once the turn ended. Each scenario began with `/clear`, which gives a new session id and therefore a fresh store and log. Because a blocked message returns to the input box, the input was cleared with Ctrl-U before each further message.

Delay means the time from pressing Enter to the intake hook writing its log line, which happens just before the hook returns.

Scenarios E to J of the M1 plan (`docs/superpowers/plans/2026-10-19-m1-trustworthy.md`, Task 11). No scenario was run twice except H, which was re-run once after the fix it called for (below).

## Summary

| Scenario | Result | Intake delay |
|---|---|---|
| E, dedupe | Pass. Resubmitted held text blocked with `that is already p2 (done)`; no new task; `next_id` unchanged | `later:` 112 ms (rule `queue_marker`); resubmit 165 ms (`dedupe`) |
| F, control channel | Pass. Five control messages blocked with their reply, `p3` cancelled, no model calls. The three-line `list` reply showed only its last line on screen | 113 / 89 / 97 / 98 / 97 ms (rules `queue_marker` x2, `control` x3) |
| G, parked and promote | Pass. `p2 parked`, nothing dispatched; after `queue: promote p2` the next Stop dispatched it and README.md was written | parked 85 ms (`parked_marker`); promote 117 ms (`control`) |
| H, amend by subject | First run: partial, the handoff dropped the note and `goodbye.py` printed only `goodbye`. Fixed (`dispatch._handoff` now carries notes) and re-run: pass, `goodbye.py` prints the date | `later:` 98 ms; amend 4.36 s (classifier `amend`, 0.93). Re-run: 93 ms; 4.68 s (0.95) |
| I, split | Pass. Steer part passed through with `act only on: name the function add_two`; `p2` queued for the subtract function; both functions in `ops.py` | 4.58 s (classifier `split`, 0.9; parts steer + queue) |
| J, `/queue` command | Pass with a deviation: the slash command is answered by the intake control rule (block, `queue is empty`), not by Claude running the CLI. No model turn | 114 ms (rule `control`) |

## Scenario E: dedupe

Typed `write a python script hello.py that prints hello, then run it`, then `later: add a goodbye.py that prints goodbye` 2.4 s later. When the turn had ended (p2 dispatched and done) and the held `later:` text still sat in the input box, pressed Enter.

Observed:
- The `later:` message was blocked and the notification `queue-switchclaude: queued p2 after p1: add a goodbye.py that prints goodbye` showed above the input box at 1.5 s. As in M0, the held text went back into the input box.
- `Ran 2 stop hooks` / `Stop hook feedback: … Do it now: add a goodbye.py that prints goodbye`; Claude wrote and ran `goodbye.py`. Store `p1 done`, `p2 done (depends_on p1)`, `next_id: 3`.
- Enter on the held text: 1.5 s later the transcript showed `UserPromptSubmit operation blocked by hook:` / `queue-switchclaude: that is already p2 (done); send 'queue: list' to see the queue`. This line is persistent (it is still there 5.5 s later), unlike the mid-run notification. Log: `decision: dedupe, task: p2, rule: queue_marker, label: dedupe`. No `new_turn` line, no new task, `next_id` still 3, state `idle`. The text went back into the input box once more; a second Enter would hit dedupe again.
- Delays: `later:` sent 10:09:43.508, logged 10:09:43.620 (112 ms). Resubmit sent 10:10:15.188, logged 10:10:15.353 (165 ms).

## Scenario F: control channel

Typed `run 6 separate bash echo commands, echo 1 through echo 6, one call at a time (sequentially, never in parallel)` (the turn lasted 20 s), then at about 2 s intervals: `later: a`, `later: b`, `queue: list`, `queue: cancel p3`, `queue: list`.

Observed:
- Every message was blocked and its reply shown as the mid-run notification: `queued p2 after p1: a`, `queued p3 after p2: b`, the listing, `cancelled p3: b`, the listing again.
- The listing has one line per live task (`p1 running …`, `p2 queued after p1 a`, `p3 queued after p2 b`) but only its last line was visible: `p3  queued   after p2   b` after the first `list`, `p2  queued   after p1   a` after the second. The notification area renders one line of a multi-line `systemMessage`. See issue 2.
- Log: the three control lines carry `decision: control, rule: control, command: list | cancel p3 | list` and no `classifier` key at all; the two `later:` lines are `rule: queue_marker, classifier: null`. No model call for any of the five.
- Store after the turn: `p1 done`, `p2 done`, `p3 cancelled`. Dispatch ran `p2` (`a`): Claude asked what task `p2` should do, since its text is just `a`, and the turn ended (`dispatch next: null`).
- Delays: `later: a` 10:10:50.942 → 51.055 (113 ms); `later: b` 53.044 → 53.133 (89 ms); `queue: list` 55.164 → 55.261 (97 ms); `queue: cancel p3` 57.292 → 57.390 (98 ms); `queue: list` 59.414 → 59.511 (97 ms). All under the 150 ms the plan asks for.

## Scenario G: parked and promote

Typed `run 3 separate bash echo commands, echo 1 through echo 3, one call at a time (sequentially, never in parallel)`, then `maybe later we can add a README` 2.4 s later.

Observed:
- Blocked at once by the rule table (`rule: parked_marker, parked: true`, no classifier). Notification: `queue-switchclaude: parked p2: we can add a README (send 'queue: promote p2' to run it)`.
- The turn ended after 7 s with `dispatch next: null`; nothing ran. Store: `p1 done`, `p2 parked (depends_on p1)`.
- Typed `queue: promote p2` at idle: blocked, shown inline as `UserPromptSubmit operation blocked by hook:` / `queue-switchclaude: promoted p2 to the queue`. Log `decision: control, command: promote p2`.
- Typed `run one bash echo that prints done` to get a Stop: `new_turn p3`, then at that turn's Stop `dispatch next: p2` and `Stop hook feedback: … Do it now: we can add a README`. Claude wrote `README.md` (17 lines describing `hello.py` and `goodbye.py`), then `dispatch next: null`. Store: `p1`, `p2`, `p3` all `done`.
- Delays: parked message 10:11:46.716 → 46.801 (85 ms); promote 10:12:07.804 → 07.921 (117 ms); the new turn's prompt 10:12:10.028 → 10.120 (92 ms); `p2` dispatched at 10:12:13.47.

## Scenario H: amend by subject

The scratch files from E and G were removed first so a fresh `goodbye.py` would be created. Typed the 6-echo task from F, then `later: add a goodbye.py that prints goodbye` 2.4 s later, then `the goodbye script should also print the date` 2.1 s after that.

First run, observed:
- `later:` blocked by `queue_marker` (98 ms): `p2 queued after p1`.
- The amend went to the classifier: `{"label": "amend", "confidence": 0.93, "target": "p2", "note": null}`, acted as `amend`, `target: p2`, `latency_ms: 4261`. Store: `p2.notes = ["the goodbye script should also print the date"]`. While the hook ran, the status line read `queue-switchclaude: sorting your message… 1/2` and the input area showed `Press up to edit queued messages` (Claude Code held the message in its own queue until the hook returned).
- The `noted on p2` confirmation was not seen on screen (captures at 1.5 s and 5.5 s after the send, and a search of the scrollback after the turn). In this and the next run a diff side panel (`No commits yet / Nothing to diff against until the repo's first commit`, then the `goodbye.py` diff) occupied the right-hand region where mid-run notifications render, and the `queued p2` notification was not visible in these runs either, although the log shows both blocks. The absence is therefore not conclusive. See issue 3.
- **Deviation:** the handoff was `Do it now: add a goodbye.py that prints goodbye` with no mention of the date, and the dispatched `goodbye.py` was `print("goodbye")`. `dispatch._handoff` used `task.text` only; the note stored by `intake._amend` never reached Claude. See issue 1.
- Delays: `later:` 10:13:23.142 → 23.240 (98 ms); amend 10:13:25.263 → 29.626 (4.36 s).

Fix (this commit, as Task 11 step 5 allows): `dispatch._handoff` appends `. The user later added: <note>[; <note>…]` when the task has notes, and is unchanged otherwise. Tests `test_dispatch_handoff_includes_notes` and `test_dispatch_handoff_without_notes_is_unchanged` in `tests/test_dispatch.py`; 323 tests pass. The Stop hook starts `dispatch.py` fresh on every call, so the running session picked the change up without a restart.

Re-run after the fix (`goodbye.py` removed first), observed:
- Same routing: `later:` 93 ms; amend classified `amend`, 0.95, `target: p2`, `latency_ms: 4592`, note stored.
- `Stop hook feedback: queue-switchclaude: the user queued task p2 to run after the previous work. Do it now: add a goodbye.py that prints goodbye. The user later added: the goodbye script should also print the date`. Claude wrote `goodbye.py` with `print("goodbye")` and `print(date.today().isoformat())`, ran it (`goodbye`, `2026-10-19`). Store `p1 done`, `p2 done` with the note.
- `noted on p2` again not seen on screen (the pane was polled every 0.3 s for 7 s after the send; the side panel was open).
- Delays: `later:` 10:15:14.888 → 14.981 (93 ms); amend 10:15:17.011 → 21.693 (4.68 s).

## Scenario I: split

Typed `write a python file ops.py with a function that adds two numbers, then run it to check 2+3`, then `name the function add_two, and after that add a subtract function` 2.4 s later.

Observed:
- The rule table returned `undecided` (`rule: null`). Classifier: `split`, 0.9, parts `[{"label": "steer", "text": "name the function add_two", 0.95}, {"label": "queue", "text": "add a subtract function to ops.py", 0.9, "depends_on": ["p1"]}]`. Acted label `split`, `parts: ["steer", "queue"]`, `latency_ms: 4476`. Store: `p2 queued after p1`, text `add a subtract function to ops.py`.
- The message passed through to the model with the additional context (confirmed in the session transcript): `queue-switchclaude: handled the rest of this message: queued p2 after p1: add a subtract function to ops.py; act only on: name the function add_two`.
- Ordering: Claude Code had queued the message while the hook ran (`ctrl+x ctrl+s to send now`, `Press up to edit queued messages`). The first turn's Stop fired at 10:16:20.19, 2.2 s after the classifier returned, before the queued message was delivered, so dispatch handed over `p2` first: Claude added `subtract`, then received the steer and renamed `add` to `add_two`, noting it had already added `subtract` from the queued task. Final `ops.py` has `add_two` and `subtract`; `dispatch next: null`; `p1`, `p2` done.
- Delay: sent 10:16:13.397, logged 10:16:17.978 (4.58 s).

## Scenario J: `/queue` command

After `/clear`, typed `/queue-switchclaude:queue list` at idle.

Observed:
- The prompt reached the `UserPromptSubmit` hook as typed. The rule table's `control` rule matches `/queue-switchclaude:queue …` as well as `queue: …`, so intake blocked it and replied inline: `UserPromptSubmit operation blocked by hook:` / `queue-switchclaude: queue is empty`. Log: one line, `decision: control, rule: control, command: list`. No `new_turn`, no model call, and `scripts/queue_cli.py` never ran.
- The plan expected Claude to run the CLI and print the queue in one short turn. The observed path is cheaper (114 ms, no turn) and gives the same answer, but it means the slash command, `commands/queue.md` and `queue_cli.py` are not reachable from the UI while the hook is installed. See issue 4.
- Supplementary check from a shell, not through Claude: with `CLAUDE_PLUGIN_DATA` pointing at the plugin's data dir, `python3 scripts/queue_cli.py list` printed `queue is empty` for the pointer session and `help` printed the command list. Without that variable the CLI uses `~/.claude/queue-switchclaude` and prints `no session found`.
- Delay: sent 10:17:12.666, logged 10:17:12.780 (114 ms).

## Issues found

1. **Amend notes were dropped from the handoff** (H, first run). `dispatch._handoff` sent `task.text` only, so `the goodbye script should also print the date` was stored on `p2` and never shown to Claude. Fixed in this commit with tests; verified live in the H re-run.
2. **A multi-line control reply shows one line.** Mid-run, the `queue: list` notification rendered only the last task line (F). `control._list` joins one line per task with newlines; the notification area shows a single line of a `systemMessage`. Either the listing needs a one-line form (for example `p1 running; p2 queued after p1: a; p3 queued after p2: b`, clipped), or the full listing needs another channel.
3. **The amend confirmation was not observed.** `noted on p2` did not appear in any capture of either H run. A diff side panel covered the notification region in those runs and also hid the `queued p2` notification that is known to work (E, F, G), so this needs one more check with the panel closed before it is called a defect.
4. **The slash command never reaches the CLI.** `/queue-switchclaude:queue list` is answered by the intake `control` rule (J). This is a better experience than a model turn, but `commands/queue.md` and `queue_cli.py` are dead from the UI's point of view, and the plan's expectation for J should be updated to the observed behaviour. If the CLI is kept as the fallback for a failed hook, note that it needs `CLAUDE_PLUGIN_DATA` to find the store.
5. **Classifier latency is 4.3 to 4.7 s** on the messages that need the model (H amend 4261 and 4592 ms, I split 4476 ms), up from 3.4 s in M0's scenario C. Claude Code holds the message in its own queue while the hook runs, and in I the turn's Stop fired before the held steer was delivered, so the queued part ran before the steer part. The outcome was still correct, but a steer sent late in a short turn can land after the work, as M0 issue 3 described.
6. **Held text still returns to the input box** after every block (M0 issue 1, second half). Dedupe now makes an accidental resubmission harmless (E), and mid-run the operator must clear the input before typing the next message or the texts concatenate. Block display also differs by state: at idle the block is a persistent `UserPromptSubmit operation blocked by hook:` line in the transcript; mid-run it is a transient notification that is gone once the turn ends.
7. **Test noise (not a plugin defect).** `README.md` written by Claude in G was removed before H along with the scratch `.py` files; the scratch repo was left with only its `.git`. The operator's own session shares the data dir and the `last_session` pointer, but no interference was seen: the pointer always named the nested session during the run.
