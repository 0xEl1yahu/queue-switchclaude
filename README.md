# queue-switchclaude

A harness for Claude Code that sorts messages typed mid-run into **steer / queue / amend / split** (with `interrupt` and `parked` flags), runs queued work in dependency order, and plans each task just in time from the previous task's handoff.

Today Claude Code injects any mid-run message into the running turn. "When you're done, list the files" runs *now*, in parallel with what it was doing. This project puts three hooks around that: intake classifies the message, a tool-call hook can hard-stop the turn, and a Stop hook dispatches the next queued task.

## Status

Design only. No code yet.

- [`docs/design-spec.md`](docs/design-spec.md): the design, reconstructed on 2026-10-18 after the original session was lost.
- [`docs/flows.md`](docs/flows.md): diagrams of the user flow, the intake decision logic, the task lifecycle, dispatch and interrupt.
- [`docs/libraries.md`](docs/libraries.md): Python libraries per problem area (store, DAG, classifier, matching, eval), with sketches.
- [`queue-harness-plan.md`](queue-harness-plan.md): the surviving side plan. Answers to the open questions, lessons borrowed from two prior projects, risks, and the M0–M3 milestones.
- [`eval/golden_queue_intake.jsonl`](eval/golden_queue_intake.jsonl): 47 labelled intake cases across five scenarios, with rationales and acceptable alternatives. The schema is in the spec, section 8.

## Next

1. Decide D1–D5 in the spec (base, classifier backend, dispatch default, done detection, Esc policy).
2. Build M0: hooks-only shadow logger.
3. Write the eval runner over the golden set and pick a classifier backend (M1).
