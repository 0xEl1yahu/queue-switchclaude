# M0 live acceptance

Run on 2026-10-18 (22:57 to 23:08 BST) in Claude Code 2.1.302 with Opus 5.5, auto permission mode, plugin loaded with `--plugin-dir` (data dir `~/.claude/plugins/data/queue-switchclaude-inline/`). The session ran in tmux in `/tmp/qsc-demo`, a fresh `git init` scratch repo. Messages were typed with `tmux send-keys`, and each send was timestamped in UTC so hook delay could be read against the `ts` of the matching decision-log line. Each scenario began with `/clear`, which gives a new session id and therefore a fresh store and log.

Delay means the time from pressing Enter to the intake hook writing its log line, which happens just before the hook returns.

## Summary

| Scenario | Result | Intake delay |
|---|---|---|
| A, queue then dispatch | Pass, except the block reason is not shown in the UI | 84 ms (rule `queue_marker`) |
| B, interrupt | Pass, except the block reason is not shown in the UI | 93 ms (rule `interrupt`); turn halted 1.92 s after the send |
| C, steer passes through | Pass | 3.38 s (classifier: `steer`, 0.95) |
| D, shadow mode | Partial: dispatch logs `next: null`, not `next: p2` | 85 ms (rule `queue_marker`) |

## Scenario A: queue then dispatch

Typed `write a python script hello.py that prints hello, then run it`, then `later: add a goodbye.py that prints goodbye` 1.9 s later.

Observed:
- The `later:` message was blocked: it never reached the model, and its text was put back in the input box. The UI did not show the reason (`queued p2 after p1: …`). It showed only a transient `Original prompt: later: add a goodbye.py that prints goodbye` line above the input. See issue 1.
- After `hello.py` ran, the transcript showed `Ran 2 stop hooks` / `Stop hook feedback: queue-switchclaude: the user queued task p2 to run after the previous work. Do it now: add a goodbye.py that prints goodbye`. Claude then created and ran `goodbye.py` with no further input.
- Store: `p1 done`, `p2 done` (`depends_on: ["p1"]`), `state: idle`.
- Log: `intake new_turn p1` → `intake label: queue, rule: queue_marker, shadow: false` → `dispatch next: p2` → `dispatch next: null`.
- Delay on the `later:` message: sent 21:59:05.961, logged 21:59:06.045, so 84 ms.

The first attempt sent the `later:` message 8 s after the first message. The first turn had already ended by then (Stop fired 1.4 s earlier), so the message became a plain new turn. That run was discarded. Fast tasks like this one leave only a few seconds for a mid-run message.

## Scenario B: interrupt

The scripted prompt `count from 1 to 50, running a separate bash echo for each number` did not exercise the interrupt. The model ran all 50 echoes as one parallel batch and the turn ended after 31 s, so `stop` arrived after the turn had finished. The prompt was reworded to `count from 1 to 50, running a separate bash echo for each number, one call at a time (sequentially, never in parallel)` and `stop` was sent once the UI showed `Ran 5 shell commands`.

Observed:
- `stop` was blocked. As in A, the UI showed `Original prompt: stop` rather than the reason, and `stop` went back into the input box.
- Echo 6 was already running. The next tool call ended the turn with `PreToolUse:Bash hook stopped continuation: queue-switchclaude: stopped as requested`.
- Store: `p1 interrupted`, `interrupt: false`, `state: idle`.
- Log: `intake label: interrupt, rule: interrupt` → `interrupt task: p1, dropped: [], via: PreToolUse`.
- Delay: sent 22:02:40.711, logged 22:02:40.805, so 93 ms. Halt logged at 22:02:42.635, 1.92 s after the send.

## Scenario C: steer passes through

Typed `write a function that adds two numbers`, then `name it add_two` 1.9 s later.

Observed:
- Not blocked. The rule table returned `undecided` (`rule: null`). The classifier returned `{"label": "steer", "confidence": 0.95}` and the final label was `steer`.
- The message reached the model after the hook returned. Claude had already written `add.py` with `def add(a, b)`, so it renamed the function. The final file contains `def add_two(a, b)`, and Claude tested it with `add_two(2, 3) == 5`.
- Store: `p1 done`. Log: `dispatch next: null`.
- Delay on the classified message: sent 22:03:30.523, logged 22:03:33.899, so 3.38 s. For those 3.4 s the turn kept running without the steer.

## Scenario D: shadow mode

`/config` → filter `shadow` → `Shadow mode · queue-switchclaude` set to `true` (it took effect without a restart). Then repeated A.

Observed:
- Nothing was blocked and nothing was dispatched, as expected. Because the message was not held, Claude Code steered `later: add a goodbye.py…` into the running turn, and Claude wrote both files in that one turn. This is stock behaviour.
- Log: `intake label: queue, rule: queue_marker, shadow: true`, then `dispatch next: null, shadow: true`.
- **Deviation:** the plan expects `dispatch next: p2` with `shadow: true`. In shadow mode `intake.handle` returns before `s.new_task(...)`, so p2 is never recorded and dispatch has no task to report. The store holds only `p1 done`. See issue 2.
- Delay on the `later:` message: sent 22:05:00.310, logged 22:05:00.395, so 85 ms.

## Issues found

1. **The block reason is not shown to the user.** In 2.1.302, a `UserPromptSubmit` block shows a transient `Original prompt: <text>` line and puts the text back in the input box. The `reason` string does not appear on screen or in the session transcript. A final check (shadow off, 10-echo task, full-screen capture 1 s after the block, then a search of the whole scrollback once the turn ended) found no `queued p2` text anywhere. The only confirmation the user gets is the later `Stop hook feedback` line. The returned text also sits in the input, so pressing Enter again would submit it as a fresh turn. M1 needs another confirmation channel, for example `systemMessage` in the hook output, or the `/queue` command.
2. **Shadow mode does not record the would-be queue.** The log has the intake decision, but dispatch cannot log what it would dispatch. A fix would be to record the task in shadow mode under a status that dispatch logs but never runs, such as `shadow_queued`.
3. **Classifier latency is enough to miss a steer.** At 3.4 s, a steer typed early in a short turn reaches the model after the work it was meant to shape, as with `add` → `add_two` in C. It still landed and was acted on.
4. **Test noise (not a plugin defect).** The plugin is also installed in the operator's own Claude Code session (`queue-switchclaude-inline`), so that session wrote its own store and log during the test. It caused no interference, because each session has its own files.
