# Changelog

One section per milestone, newest first. Each milestone was accepted live in Claude Code 2.1.302 and the record is linked from its section. Versions are the `version` field of `.claude-plugin/plugin.json`; M2 shipped on the M1 version number.

## 0.4.0, M4 daily-use polish (2026-10-19)

- Pass-throughs that tell Claude something the user should also know now carry a one-line `systemMessage` next to the `additionalContext`, so `nothing was queued, running now: …`, `Esc dropped p2, p3`, `p2 is queued again`, `queued p2 after p1: …` (the split case), `running p2`, `revising the plan for p2` and `p2 confirmed done` appear on screen; what Claude reads is unchanged.
- `hook_ms` is measured from the first Python statement of each entry script, so it covers the `qsc` imports as well as the work; a missing or future start stamp reads `0`, never a negative number. The shadow report also prints the interpreter's own start-up cost (`python3 start-up: N ms`), which `hook_ms` cannot include.
- Garbage collection prunes `pointers/` files that name a session with no store and no log, and never the pointer of a live session, even one whose first message was a pass-through; `gc.collect` returns `(deleted_sessions, deleted_pointers)`.
- The interrupt script reads `PreToolUse` only and ignores a `PostToolUse` event; `queue: help` names every control word the rules accept.
- README rewritten for daily use (install as the UI walks it, requirements, a week of Shadow compare, the known limits in one list); this changelog; the two manifests carry the same description.

## 0.3.0, M3 hardening (2026-10-19)

- Installs from GitHub through the plugin marketplace (`/plugin marketplace add 0xEl1yahu/queue-switchclaude`); MIT licence; CI on Python 3.10, 3.12 and 3.14; the eval cache is tracked so a fresh clone reproduces the golden-set gate offline.
- Each working directory keeps its own session through a pointer file (hashed from the canonical path, resolved up through the parents), so two sessions in different folders never see each other's queue; the first Stop of a session deletes sessions untouched for 30 days, never its own.
- At idle the markers do the sensible thing: `later: x` runs now with the marker stripped (or queues `x` behind work that is already waiting), `maybe later …` parks the task without a turn, and `stop` answers `nothing is running`. A marker or `stop` typed right after Esc goes through the same rules with the Esc notice attached.
- Esc during a planning turn puts the task back in the queue with its half-written plan deleted and nothing else dropped; the retry is worded differently from the first handover, so the spin guard (an identical handback twice) lets it through.
- Harness-generated prompts (task notifications, skill invocations, interrupted-request markers) never become tasks; a verify command that runs past 120 s is killed with its whole process group; the interrupt hook is registered on `PreToolUse` only; the auto handover is idempotent; a verify handback no longer prints an empty `Output tail:`.
- The shadow report prints hook latency p50 and p95 per hook, split by whether a model call was made, and a count per intake decision. Live record: `docs/m3-acceptance.md` (scenarios T to W, both marketplace forms).

## M2 plan flow (2026-10-19, shipped on 0.2.0)

- Just-in-time plans: at the Stop that closes a task, dispatch writes `handoffs/pN.md`, hands the next task over for planning, and Claude writes `plans/pN.md`, presents it in a few lines and stops; `lgtm`, `ok`, `go`, `continue`, `ship it` or `queue: approve` runs it, `looks good but …` revises it, an unrelated message runs as its own turn after which the plan is shown again, and `queue: auto pN` skips the gate.
- Done is decided by the task's verify command (`queue: verify pN <command>`, exit 0 means done) or, when none is set, by a haiku judge over Claude's closing message; a "not finished" verdict is handed back at most twice, then a failing verify sets the task aside as `failed_verify` (promote retries, cancel drops). A turn the user typed closes as `stopped` with no model call.
- When Claude stops by asking a question, the next message is the answer and the task resumes; `ok, merge` confirms it done; a planned child whose parent changed is re-planned.
- `queue: show`, `verify`, `auto`, `manual` and `approve`; the verify or judge runs before the store lock is taken, so a long verify no longer holds up intake; a continuation budget of six pauses an automatic chain with a visible message; the slash command's CLI finds the data dir without the hook's environment variable.
- Live records: `docs/m2-acceptance.md` (scenarios K to O) and `docs/m2-fixes-acceptance.md` (P to S after the review follow-ups).

## 0.2.0, M1 trustworthy (2026-10-19)

- The v2 decision contract: `amend` (a note on an existing task, matched by subject with stdlib similarity and an accept and margin threshold), `split` (a message that is partly a steer and partly a queued task), a `parked` flag for `maybe later …`, and `interrupt` as a flag on `steer`.
- The `queue:` control channel (`list`, `cancel`, `promote`, `park`, `drop`, `pause`, `resume`, `help`) answered in place without a model call, plus the `/queue` slash command and `queue_cli.py`; the slash form is caught by the same rule before the CLI runs.
- Dedupe: held text resubmitted from the input box is blocked with `that is already pN (…)` instead of becoming a second task; pause-aware dispatch; Esc drops the queue with a notice listing what was dropped.
- Golden-set eval (`eval/run_golden.py`, 47 cases, asymmetric dependency scoring): 44 of 47 with zero missed hard dependencies and both interrupt cases correct, the gate M2 to M4 keep; shadow compare mode logs the model's answer next to the rule's and `eval/shadow_report.py` summarises the disagreements.
- Review follow-ups: steer-with-pause pauses the named task, the control rule needs `queue:` or a known command word, `promote` and `resume pN` bring a paused task back, `list` and `help` are one line because the notification shows one. Live record: `docs/m1-acceptance.md` (scenarios E to J); eval record: `docs/m1-eval.md`.

## 0.1.0, M0 vertical slice (2026-10-18)

- Three classic hooks packaged as a Claude Code plugin: `UserPromptSubmit` intake (deterministic rules first, then a `claude -p` classifier with a JSON schema that fails open to steer), a `PreToolUse` interrupt that stops the turn when you say `stop`, and a Stop-hook dispatch that hands the next ready task to Claude in dependency order.
- A per-session JSON store with an `fcntl` lock and atomic saves, a JSONL decision log, a transcript tail reader, and a **Shadow mode** setting that logs every decision without acting on it.
- Acceptance fixes: a block carries `systemMessage` and `suppressOriginalPrompt` so the reason is shown on screen (2.1.302 hides the block `reason`); shadow mode records `shadow_queued` and `shadow_dispatched` so the report can see what would have run; `stop` drops the queued tasks and names them; session ids are sanitised before they become file names.
- Live record: `docs/m0-acceptance.md` (scenarios A to D and the re-run). The two points left open there, held text returning to the input box after a block and the 3 to 4 s classifier call, are listed under "Known limits" in the README.
