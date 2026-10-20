# Changelog

One section per milestone, newest first. Each milestone was accepted live in Claude Code 2.1.302 and the record is linked from its section. Versions are the `version` field of `.claude-plugin/plugin.json`; M2 shipped on the M1 version number.

## 0.5.0, native Windows and review fixes (2026-10-20)

No live acceptance run for this release: everything below is covered by the test suite (CI on Ubuntu and Windows), not by a recorded session.

### Review fixes

Fixes from the 2026-10-20 code review, each with a test that failed first.

- **Dependants are no longer stranded.** A task that waited on one that was cancelled, dropped or interrupted now waits on what that task waited on (`Store.rewire`): `queue: cancel p2` with `p3` after `p2` after `p1` leaves `p3` after `p1`, and the reply says `p3 now after p1`; `queue: drop`, an amend that cancels, Esc and `stop` rewire the same way. The model's `depends_on` is filtered to tasks that exist and will still run. A `failed_verify` task still holds its dependants until it is promoted or cancelled, and its notice now names them (`p3 waits on it`).
- **The classifier no longer runs under the store lock.** Intake builds the payload under the lock, releases it for the `claude -p` call, then reruns on a fresh load and uses the answer only if the state, `current_prompt_id`, running and pending task are unchanged; otherwise it asks once more, then fails open. The interrupt check on `PreToolUse` (5 s timeout) no longer waits behind a 15 s classification, and writes other hooks make meanwhile are kept.
- **`stop` and Esc reset a turn the same way** (`interrupt.reset_turn`). While a task runs, it is interrupted and queued tasks are dropped (D5); while a plan is being written, that task is queued again and nothing is dropped. A plan waiting for approval is kept either way: a typed `stop` used to drop it.
- **Split-part ids** are predicted from the same tasks the payload shows (`running_task` and `queue`), so a planned or failed-verify task the model never saw no longer shifts a part's dependency by one.
- **Retry handbacks respect the continuation budget.** With 6 continuations spent, a "fix it" or "finish it" handback is withheld (the task waits on the user; any message resumes it, `ok` confirms it done) and a planning retry is not sent (the task is queued again), so six continuations plus two retries can no longer reach Claude Code's cap of 8.
- **Report wiring.** The interrupt hook logs `hook_ms` and the report lists it; dispatch logs the judge's `latency_ms` (a verify command's `verify_ms`) so the Stop hook's "model called" bucket is filled; a line counts as model-decided only when a model call was made for a message the rules left open. `/queue approve` prints the execution handover on its own line, as `commands/queue.md` says, and makes the turn it runs in the current one (intake records the last `prompt_id` per session) with the interrupt flag cleared.

### Native Windows

- Runs on native Windows, not only WSL. Before this every hook exited silently on Windows, because the store imported `fcntl` at the top. The store lock is now `msvcrt.locking` on byte 0 of the `.lock` file there (polled, so a writer waits as long as it would on `flock`), and a save retries the rename for up to a second while another process has the store open. macOS and Linux keep `fcntl` and the same code paths as before.
- A verify command runs under `cmd.exe` on Windows, in its own process group, and on timeout its whole tree is killed with `taskkill /F /T`. Before, the missing `os.killpg` raised an error that crashed the Stop hook.
- The classifier and the judge find `claude` on `PATH` with `shutil.which`, so an npm install (`claude.cmd`) works. On Windows the system prompt goes by `--system-prompt-file` and the payload on stdin: the command line stays under Windows' 32767-character cap, and nothing you type passes through `cmd.exe`. A call that times out kills the launcher's tree, not only `cmd.exe`. A batch launcher is refused if an argument holds a character `cmd.exe` would act on; the shipped schemas hold none.
- The background comparison starts detached on Windows (`DETACHED_PROCESS`), hook stdin is decoded as UTF-8 there instead of the ANSI code page, the `/queue` output is printed as UTF-8, and pointer files fold the case of the working directory, so `C:\Proj` and `c:/proj` share one queue.
- The hooks still run `python3` in exec form. On Windows that has to be a real interpreter (the Python install manager and the Store Python provide one; the `WindowsApps` stub does not); README Requirements has the details and why `python` or a plugin option cannot replace it. `/queue` gains a PowerShell form for Windows without Git Bash.
- CI adds Python 3.12 on `windows-latest`. Tests that use sh syntax or POSIX process groups are skipped there; a `claude.cmd` launcher around a Python fake stands in for the model, and new tests cover the Windows command line, a killed grandchild, the lock probe and Windows path spellings. No live Claude Code session on Windows has run the plugin yet.

## 0.4.1 (2026-10-19)

- **Shadow: also ask the model** now works while the plugin is active, not only in shadow mode. For a message the rules decided, intake starts `scripts/compare.py` detached; it asks the classifier and logs the answer as a `shadow_compare` line beside the `intake` line that acted. The rule path keeps its ~90 ms; the comparison costs one background `claude -p` call. `eval/shadow_report.py` counts these lines in "compared" and "disagreements". Checked by hand in the installed plugin (rule path 35 ms, the comparison line 4.6 s later); no written acceptance record.

## 0.4.0, M4 daily-use polish (2026-10-19)

- Pass-throughs that tell Claude something the user should also know now carry a one-line `systemMessage` next to the `additionalContext`, so `nothing was queued, running now: …`, `Esc dropped p2, p3`, `p2 is queued again`, `queued p2 after p1: …` (the split case), `running p2`, `revising the plan for p2` and `p2 confirmed done` appear on screen; what Claude reads is unchanged.
- `hook_ms` is measured from the first Python statement of each entry script, so it covers the `qsc` imports as well as the work; a missing or future start stamp reads `0`, never a negative number. The shadow report also prints the interpreter's own start-up cost (`python3 start-up: N ms`), which `hook_ms` cannot include.
- Garbage collection prunes `pointers/` files that name a session with no store and no log, and never the pointer of a live session, even one whose first message was a pass-through; `gc.collect` returns `(deleted_sessions, deleted_pointers)`.
- The interrupt script reads `PreToolUse` only and ignores a `PostToolUse` event; `queue: help` names every control word the rules accept.
- README rewritten for daily use (install as the UI walks it, requirements, a week of Shadow compare, the known limits in one list); this changelog; the two manifests carry the same description.
- Live record: `docs/m4-acceptance.md` (scenarios X to AA; AA found the mid-turn block notice is not drawn in 2.1.302 with the side panel open).

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
