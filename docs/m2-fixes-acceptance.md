# Live re-run after the M2 review follow-ups

Run on 2026-10-19 (12:53 to 13:02 BST) against commit `64a23d0` plus the data-dir fix committed with this file. Claude Code 2.1.302, Opus 5.5, auto permission mode, `--plugin-dir`, tmux in `/tmp/qsc-demo`, each scenario after `/clear`. Delay is send-to-log-line. The driver clears the input line before every send, because a blocked prompt returns its text to the input box (M0 issue 1); the first attempt at Q concatenated three messages into one for exactly that reason and was discarded.

| Scenario | Result | Evidence |
|---|---|---|
| P, plain question turn | Pass. No judge call: the turn closed with `verdict: stopped, reason: prompt turn ended` 2.2 s after the send (the model's own answer time), store `p1 done` with `kind: prompt`. | log `handoff … stopped` |
| Q, failing verify set aside | Pass. `queue: verify p2 python3 -c "import sys; sys.exit(1)"` (90 ms), `lgtm` ran the plan; two handbacks (`attempts 1, 2`), then `p2 failed_verify`, no `p2.md` handoff, and the message `p2 failed its verify command 2 times and is set aside as failed_verify (send 'queue: promote p2' to retry it, 'queue: cancel p2' to drop it)` shown on screen as `Stop says:`. | log `dispatch … failed_verify: true` |
| R, "continue" while a plan waits | Pass. `continue` logged as `decision: approve, rule: non_task`; the execution handover followed; `b.txt` written; `p2 done` by the judge. | 74 ms |
| S1, `/queue-switchclaude:queue approve` | Pass. The intake control rule caught the slash form (`decision: control, approved: p2`), the execution handover rode on the prompt, `d.txt` written, `p2 done`. Claude also ran the slash command's CLI, which printed `No session found` (fixed below). | 70 ms |
| S2, the CLI path | Fixed and verified. First run: `queue_cli.py approve` printed `no session found` because the Bash tool's environment has no `CLAUDE_PLUGIN_DATA`, so the CLI looked in `~/.claude/queue-switchclaude`. After the fix (the command file passes `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}"`, and `paths.data_dir()` falls back to the newest `~/.claude/plugins/data/queue-switchclaude*` directory), the re-run logged `control: approve via queue_cli` against the right session. The CLI's own approval transition could not be isolated: the prompt that asked Claude to run it contains "approve" and intake's classifier approved the plan first (`steer`, note approve, 0.9), so the CLI then answered `nothing is waiting for approval`. The realistic paths (`lgtm`, `continue`, `queue: approve`, the slash command) all go through intake and pass. | log `control … via: queue_cli` |

## Observations

- Every Stop in P to S took under a second except where a judge ran (about 4 s) or a verify command ran.
- The `Output tail: .` rendering for an empty verify output is still there (reviewer nit).
- `/clear` with held text in the input box still needs the line cleared first; the driver does that now.
