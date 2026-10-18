# queue-switchclaude

A harness for Claude Code that sorts messages typed mid-run into **steer / queue / amend / split** (with `interrupt` and `parked` flags), runs queued work in dependency order, and plans each task just in time from the previous task's handoff.

Today Claude Code injects any mid-run message into the running turn. "When you're done, list the files" runs *now*, in parallel with what it was doing. This project puts three hooks around that: intake classifies the message, a tool-call hook can hard-stop the turn, and a Stop hook dispatches the next queued task.

## Status

Design decided, no code yet. The five open decisions were made on 2026-10-18: build fresh, classic hooks in Python packaged as a plugin, classifier through `claude -p` with rules first, approve each plan, verify-or-judge for done, Esc drops the queue with a notice. See [`docs/decisions.md`](docs/decisions.md).

- [`docs/design-spec.md`](docs/design-spec.md): the design, reconstructed on 2026-10-18 after the original session was lost.
- [`docs/flows.md`](docs/flows.md): diagrams of the user flow, the intake decision logic, the task lifecycle, dispatch and interrupt.
- [`docs/decisions.md`](docs/decisions.md): D1 to D5, with the evidence and the rejected options.
- [`docs/libraries.md`](docs/libraries.md): Python libraries per problem area (store, DAG, classifier, matching, eval), with sketches.
- [`queue-harness-plan.md`](queue-harness-plan.md): the surviving side plan. Answers to the open questions, lessons borrowed from two prior projects, risks, and the M0–M3 milestones.
- [`eval/golden_queue_intake.jsonl`](eval/golden_queue_intake.jsonl): 47 labelled intake cases across five scenarios, with rationales and acceptable alternatives. The schema is in the spec, section 8.

## Next

Working product first (spec §10):

1. **M0 vertical slice**: steer / queue / interrupt end to end in one session, chain dependencies, dispatch on Stop, rules-first classification via `claude -p`, a shadow switch.
2. **M1 trustworthy**: golden-set eval, amend and split, parked, Esc handling, `/queue` command.
3. **M2 plan flow**: just-in-time plans, approval gate, handoffs, verify-or-judge.
4. **M3 hardening**.
