# queue-switchclaude

A harness for Claude Code that sorts messages typed mid-run into **steer / queue / amend / split** (with `interrupt` and `parked` flags), runs queued work in dependency order, and plans each task just in time from the previous task's handoff.

Today Claude Code injects any mid-run message into the running turn. "When you're done, list the files" runs *now*, in parallel with what it was doing. This project puts three hooks around that: intake classifies the message, a tool-call hook can hard-stop the turn, and a Stop hook dispatches the next queued task.

## Status

Design only. No code yet.

- [`docs/design-spec.md`](docs/design-spec.md): the design, reconstructed on 2026-10-18 after the original session was lost.
- [`queue-harness-plan.md`](queue-harness-plan.md): the surviving side plan. Answers to the open questions, lessons borrowed from two prior projects, risks, and the M0–M3 milestones.

The 47-case golden set (`golden_queue_intake.jsonl`) was lost with the original session and has to be rebuilt. Milestone M0 (a shadow logger) exists partly to harvest real messages for it.

## Next

1. Decide D1–D5 in the spec (base, classifier backend, dispatch default, done detection, Esc policy).
2. Build M0: hooks-only shadow logger.
3. Rebuild the golden set and the eval runner (M1).
