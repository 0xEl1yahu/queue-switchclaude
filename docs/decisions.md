# Decisions

Decided 2026-10-18 in conversation. Each entry: the decision, the options rejected, the evidence.

## D1. Base: build fresh

Own code, no fork. Jev_steer_or_queue's hook tricks are now in the official hooks reference (`prompt_id`, `continue: false`, Stop `additionalContext`, `last_assistant_message`), its queue is a FIFO and its classifier is coupled to TypeSafe, so most of its 300 lines would be replaced. claude-task-manager's non-blocking philosophy and word-overlap matching are what the plan found wanting.

## D2. Runtime: classic hooks, all Python, packaged as a plugin

The harness is a Claude Code plugin whose `hooks/hooks.json` declares `command` hooks on `UserPromptSubmit`, `Stop` and `PreToolUse`/`PostToolUse`, each running a Python script under `${CLAUDE_PLUGIN_ROOT}`.

Rejected: a mod (in-process JavaScript hooks module). It would have given instant interrupt (`$.turn.abort`), a clean dispatch turn (`$.prompt.submit`), a live pane, `turn.complete` with Esc detection, and a native mid-turn flag (`turnId` on `prompt.submit`). It was rejected because the core would have to be JavaScript and the mod surface is marked early access. If the classic-hook limits bite (interrupt only at tool boundaries, no Esc signal, dispatch via `additionalContext`), the mod route is the documented upgrade path; the reference is in [libraries.md](libraries.md) and the type declarations at `mods/types/claude-code.d.ts` in the claude-code repo.

Also rejected: a `prompt`-type hook as the classifier. On `UserPromptSubmit` it can only return `{ok, reason}`, and `ok: false` ends the turn with a warning line. No structured output, no store access. It is a gate, not a router.

## D2b. Classifier caller: `claude -p`, rules first

The intake script shells out to:

```
claude -p --model haiku --setting-sources "" --strict-mcp-config --tools "" \
  --output-format json --json-schema <Decision schema> --system-prompt <classifier prompt> <payload>
```

Uses the user's Claude Code auth. No API key, no third party.

Measured on this machine, 2026-10-18, Claude Code 2.1.296:

| Call | Model time | Wall-clock | Cost |
|---|---|---|---|
| one-word label, no schema, run 1 | 3.8 s | 5.5 s | $0.0002 |
| one-word label, no schema, run 2 | 3.2 s | 4.8 s | $0.0002 |
| golden case g05 with the JSON schema | 2.1 s | 3.6 s | $0.0004 |

g05 came back `{"label":"queue","depends_on":["p1"],"interrupt":false,"parked":false,"confidence":0.9}`, which is the expected answer.

That is 3 to 5 s per classified message, against the plan's 1 to 2 s hope, so the model is called only when rules cannot decide:

1. Harness traffic and non-tasks pass through (plan §3).
2. State `idle` with an empty queue: everything actionable is a new task, pass through and record.
3. Explicit markers decide without the model: "stop" alone or "stop that now" (interrupt), `pN:` prefix, `later:` prefix (queue), "maybe later" (parked), a numbered list (split), a lone `.` (accept).
4. Everything else goes to the model.

Rejected: Anthropic API key direct (sub-second, but a key on every machine and separate billing); Ollama on the lab PC (latency and quality unproven, server must be reachable).

Open: measure what fraction of the golden set the rules alone decide, and whether `claude -p` start-up can be trimmed (a warm daemon is not available to hooks).

## D3. Dispatch default: approve each plan

When a task becomes ready, the Stop hook writes `plans/pN.md`, has Claude present it, and ends the turn with state `awaiting_approval`. "lgtm" runs it; "looks good but do X first" revises it without approving (golden cases g44 to g47). A task can opt out per message ("run it without asking") or be marked `auto` in the queue.

Rejected: auto-run by default (errors in dependency inference compound before the user sees them; the Stop-hook continuation cap of 8 only bounds runaway chains, it does not prevent wrong work). Rejected: size-based auto-run (an extra judgement the golden set does not cover).

## D4. Done detection: verify command if set, else judge

A task may carry a verify command; if it exits 0 on Stop, the task is done. Otherwise the Stop hook runs a haiku judge through `claude -p` over `last_assistant_message` (and the plan), answering done / not done / asked a question. The user's next message can also confirm: "ok, merge" marks the previous task done. The judge runs inline: the Stop hook has a 600 s default timeout and the call takes about 4 s, so no detached process.

Rejected: judge only (a passing closing message is not a passing test). Rejected: verify command only (every untested task would wait on the user).

## D5. Esc: drop with a notice

When the next message arrives with a new `prompt_id` while state still says `running` and no Stop fired for that task, the harness treats the turn as interrupted: the interrupted task is marked `interrupted`, every `queued` and `ready` task is dropped, and a notice is attached to the next prompt as `additionalContext` listing what was dropped (ids and intents) so the user can re-queue by pasting. Assumption, not yet confirmed: `parked` items survive, because they never dispatch on their own; `done` history survives.

Rejected (my recommendation): pause and keep everything. Rejected: ask on the next message. The user chose drop for simplicity and predictability: Esc means "stop the plan".
