# M4 live acceptance

Run on 2026-10-19 (16:37 to 16:43 UTC, 17:37 to 17:43 BST) against commit `c479d6a` (plugin version 0.4.0). Claude Code 2.1.302, Opus 5.5 (high effort), auto permission mode, tmux 150x45 in `/tmp/qsc-demo` with `--plugin-dir` from the main checkout (data dir `~/.claude/plugins/data/queue-switchclaude-inline/`). Same method as `docs/m2-fixes-acceptance.md` and `docs/m3-acceptance.md`: each scenario after `/clear`; the driver clears the input line with Ctrl-U before every send because a blocked prompt returns its text to the input box (review focus 5); sends timestamped in UTC just before Enter; pane captured 0.6 s and 1.5 s after each send and again when the turn ended (polled every 3 s until `esc to interrupt` was gone and the pane was stable for 4 s); in Z and AA the pane was also polled every 0.5 s for `queue-switchclaude` lines. Delay is send-to-log-line. Scenarios X to AA of the M4 plan (`docs/superpowers/plans/2026-10-19-m4-daily-use.md`, Task 5). No plugin code was changed; 651 tests passed before the run. Each scenario was run once.

Two send stamps (the second `lgtm` in X and `stop` in Y) were lost to the driver's output trimming; their log lines are given instead and the delays are not reported.

## Summary

| Scenario | Result | Evidence |
|---|---|---|
| X, paused queue | Pass, with one note on the plan's expected text. `later: write a.txt containing alpha` queued p2 mid-run; p1 closed (`stopped`), p2 planned, state `awaiting_approval`. `queue: pause` → blocked `queue paused`; `later: add a README` → blocked `queued p3 after p2: add a README`, store `p3 queued` after `p2`, nothing ran. `queue: resume` → `queue resumed`; `lgtm` → `running p2` on screen, `a.txt` written, judge `done`, p3 planned at that Stop; `lgtm` → `running p3`, `README.md` written, judge `done`. Store `p1, p2, p3 done`, p3 after p2. The `(nothing is running; send 'queue: list' to see why)` suffix the plan expected belongs to the idle path; the state here was `awaiting_approval`, so the waiting-state confirmation was shown instead (observation 2). | p1 122 ms; `later:` 100 ms; `pause` 109 ms; `later:` 94 ms; `resume` 96 ms; `lgtm` 97 ms |
| Y, stop after Esc | Pass. Esc 4.1 s into a three-file turn (one file written, `Interrupted · What should Claude do instead?`); `stop` → blocked `nothing is running`; log `esc_reset, interrupted: p1, dropped: []` then `decision: noop`; no task created (`next_id` 3 after the following question), `interrupt: false`. `what is 2+2` ran as a normal turn (p2 `done`, `stopped`, no judge). Nothing was queued, so there was no `Esc dropped` prefix. | p1 163 ms; `stop` logged 16:40:20.492; question 94 ms |
| Z, later after Esc | Pass. Esc 4.1 s into a three-file turn; `later: add goodbye.py` → log `esc_reset, interrupted: p1` then `new_turn, task: p2, marker_stripped: true`; `UserPromptSubmit says: queue-switchclaude: nothing was queued, running now: add goodbye.py` on screen from 5.2 s after the send (first poll that saw it) and still there at the turn end; `goodbye.py` written; store `p1 interrupted`, `p2 done` with text `add goodbye.py`. | p1 102 ms; `later:` 96 ms |
| AA, the notices | Pass for the pass-through notices, with one finding. `running p2` appeared at approval (`UserPromptSubmit says: queue-switchclaude: running p2`, within 0.6 s of `lgtm`) and the idle run-now message appeared in Z. The mid-turn block `queued p2 after p1: add a bye.py that prints bye` was not shown on screen at any poll (0.6 s, 1.5 s, every 0.5 s for 8 s, turn end); the held text came back to the input box and the log has the `queue_marker` line. Observation 1. | p1 116 ms; `later:` 90 ms; `lgtm` 96 ms |

## Scenario X: paused queue

Session `7a033eae`. Typed `write hello.py that prints hello, then run it, one call at a time`, then `later: write a.txt containing alpha` 2.0 s later, waited for the plan, then `queue: pause`, `later: add a README`, `queue: resume`, `lgtm`, and `lgtm` again once p3 was planned.

Observed:
- The `later:` at 16:37:44.193 was logged at 44.293 (`rule: queue_marker, label: queue`). Its confirmation was not on screen at 1.5 s (see AA); the text was back in the input box. p1 closed at the Stop (`handoff p1, verdict: stopped, reason: prompt turn ended`, no judge), `dispatch next: p2`, and the planning handover ran as Stop hook feedback: `plans/p2.md` written, plan presented, `dispatch planned: p2`, state `awaiting_approval`, `pending_task: p2`.
- `queue: pause` (16:38:27.284 → 27.393): `UserPromptSubmit operation blocked by hook: queue-switchclaude: queue paused`, log `decision: control, command: pause`, store `paused: true`.
- `later: add a README` (31.371 → 31.465): blocked with `queued p3 after p2: add a README`, log `decision: queue, state: awaiting_approval, task: p2, rule: queue_marker`, store `p3 queued, depends_on: [p2]`. No model call, no file, state still `awaiting_approval`.
- `queue: resume` (43.811 → 43.907): blocked `queue resumed`, `paused: false`. A control block starts no turn, so nothing was dispatched by it; the plan for p2 was still the pending one.
- `lgtm` (47.873 → 47.970): log `decision: approve, task: p2`; `UserPromptSubmit says: queue-switchclaude: running p2` on screen at 0.6 s; `a.txt` written. At the Stop the judge closed p2 (`verdict: done`, 3.3 s), `dispatch next: p3`, and p3 was planned (`planned: p3`, 16:39:06.5).
- `lgtm` (logged 16:39:22.572): `running p3` on screen, `README.md` written listing both files, judge `done`, `dispatch next: null`. Store at the end: `p1 done`, `p2 done (after p1)`, `p3 done (after p2)`, state `idle`, `paused: false`.

The plan's expected text for the `later:` after the pause, `queued p3 after p2 … (nothing is running; send 'queue: list' to see why)`, is the message intake gives at idle when work is waiting (`test_idle_later_with_paused_queue_queues_after_it`). With the steps as written, p2 had already been planned, so the store was `awaiting_approval` and the shorter waiting-state confirmation applied. Either way the task was queued after p2 and nothing ran, which is what the scenario checks. The idle variant was not run, to keep model calls down.

## Scenario Y: stop after Esc

Session `dfa1aefb`. Typed `write b.txt, c.txt and d.txt each containing its own name, one call at a time`, Esc at 4.1 s, then `stop`, then `what is 2+2`.

Observed:
- At the Esc the turn had written `b.txt` only; the pane showed `Interrupted · What should Claude do instead?`. No log line at the Esc itself (there is no hook for it).
- `stop` (logged 16:40:20.492): `UserPromptSubmit operation blocked by hook: queue-switchclaude: nothing is running` at 0.6 s and 1.5 s. Log `decision: esc_reset, interrupted: p1, dropped: []` then `decision: noop, state: idle, rule: interrupt`. Store `p1 interrupted`, `interrupt: false`, no new task.
- `what is 2+2` (16:40:34.095 → 34.189): log `new_turn, task: p2`; answered in 2 s; `handoff p2, stopped, prompt turn ended`; `dispatch next: null`. Store `p2 done`, `next_id: 3`, so `stop` created nothing.
- After the answer Claude Code's own prompt suggestion (`yes, finish c.txt and d.txt`) sat in the input box; the driver's Ctrl-U cleared it before the next send. It is unrelated to the plugin.

## Scenario Z: later after Esc

Session `4f9d08ac`. Typed `write e.txt, f.txt and g.txt each containing its own name, one call at a time`, Esc at 4.1 s (after `e.txt`), then `later: add goodbye.py`.

Observed:
- `later: add goodbye.py` (16:41:25.398 → 25.494): log `esc_reset, interrupted: p1, dropped: []` then `new_turn, task: p2, marker_stripped: true`. The prompt passed through; the pane showed `❯ later: add goodbye.py` followed by `⎿ UserPromptSubmit says: queue-switchclaude: nothing was queued, running now: add goodbye.py` (first seen at 16:41:30.6, the first 0.5 s poll after the turn started rendering; not yet at 0.6 s or 1.5 s), and the line was still there at the turn end. Claude wrote `goodbye.py` (`print("goodbye")`) and mentioned the unfinished `f.txt` and `g.txt`.
- Stop: `handoff p2, stopped, prompt turn ended`, `dispatch next: null`. Store `p1 interrupted`, `p2 done`, text `add goodbye.py` (marker stripped), kind `prompt`, state `idle`.

## Scenario AA: the notices

Session `a65c9e61`. M0 scenario A with new names: `write a python script hi.py that prints hi, then run it`, then `later: add a bye.py that prints bye` 2.0 s later, then `lgtm` once the plan was up.

Observed:
- `later:` (16:42:09.027 → 09.117, `rule: queue_marker`): no `queue-switchclaude` line anywhere in the pane at 0.6 s, at 1.5 s, or in any 0.5 s poll until the Stop hook feedback appeared at 5.2 s; no `Original prompt:` line either. The held text was back in the input box and stayed there through the planning turn. The hook did block the prompt (the log line, no model call for it) and the store has `p2 queued after p1`.
- Stop: `handoff p1, stopped`, `dispatch next: p2`, planning handover as `Stop hook feedback: queue-switchclaude: next task is p2: add a bye.py …`, `plans/p2.md` written, plan presented, `planned: p2`.
- `lgtm` (56.694 → 56.790): `⎿ UserPromptSubmit says: queue-switchclaude: running p2` under the prompt at 0.6 s; `bye.py` created and run (`bye`); judge `done` (3.4 s); `dispatch next: null`. Store `p1 done`, `p2 done (after p1)`, kind `task`.

Which `systemMessage` lines appeared on screen in this run:

| Source | Hook output | On screen |
|---|---|---|
| mid-turn `later:` (X, AA) | block + `systemMessage: queued p2 after p1: …` | No. Not at 0.6 s, 1.5 s, any 0.5 s poll, or the turn end. |
| control at rest (`queue: pause`, `resume`) | block + `systemMessage` | Yes, `UserPromptSubmit operation blocked by hook: queue-switchclaude: …` |
| `later:` while a plan waits (X) | block + `systemMessage: queued p3 after p2: …` | Yes, same rendering |
| `stop` at idle after Esc (Y) | block + `systemMessage: nothing is running` | Yes, same rendering |
| approval `lgtm` (X twice, AA) | pass-through + `systemMessage: running pN` | Yes, `⎿ UserPromptSubmit says: queue-switchclaude: running pN` under the prompt, within 0.6 s |
| idle run-now after Esc (Z) | pass-through + `systemMessage: nothing was queued, running now: …` | Yes, same rendering, once the turn started rendering (about 5 s) |
| Esc notices (`Esc dropped …`, `pN is queued again`) | not exercised: nothing was queued at the Esc in Y or Z | — |

## Observations

1. The one `systemMessage` that does not reach the screen is the mid-turn block. In the M0 re-run (`docs/m0-acceptance.md`, scenario A) the same message was visible above the input box while the turn ran; in 2.1.302 with this layout (the pane had Claude Code's diff side panel open on the right) it was never drawn. Block messages at rest and every pass-through `systemMessage` render. So a user who queues mid-run sees only the held text come back; the first confirmation they get is the Stop hook feedback a few seconds later. Worth a line in the README until the UI shows it, or a different channel for the mid-turn case.
2. Task 5's expected text for scenario X describes the idle path (queue paused before the next task is planned). The steps as written leave the store in `awaiting_approval`, where intake's waiting-state rule gives `queued p3 after p2: add a README` without the suffix. Both are correct behaviour; the plan text should name the one it means.
3. `queue: resume` is a block, so it dispatches nothing by itself; the paused work moves when the next turn ends or the pending plan is approved. In X the approval did it.
4. Every hook in X to AA reported `hook_ms` 22 to 51 except the three judge runs (3.3 to 3.4 s); end-to-end delays were 90 to 163 ms. `hook_ms` now counts from the first Python statement (M4 Task 2), which is why the numbers are 30 to 40 ms rather than the 1 to 7 ms of M3: that is the `qsc` import cost, and the remaining 60 to 120 ms is process start plus Claude Code's own hook plumbing.
5. Claude Code 2.1.302 puts a suggested follow-up (`yes, finish c.txt and d.txt`) in the input box after some turns. The driver's Ctrl-U handles it; a human would need to clear it too before typing a `queue:` command.
