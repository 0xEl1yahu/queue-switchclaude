# Python libraries by problem area

> **After decision D2b (2026-10-18):** the classifier is called through `claude -p --model haiku --json-schema`, not the `anthropic` SDK, Ollama or the Agent SDK. Those three rows and their sketches below are kept as the rejected alternatives. The hook plumbing, store, DAG, matching and eval rows stand. The mod section at the end is the documented upgrade path if classic hooks prove too limiting.

Checked against PyPI on 2026-10-18. Local interpreter is Python 3.14, so every stdlib item below is available. Versions are the current releases on that date; pin at install time.

The rule for choosing: stdlib first, one small dependency per problem, nothing heavy on the inline hook path (intake has a ~1–2 s budget and runs on every message).

## Summary

| Problem area | Pick | Why | Alternative |
|---|---|---|---|
| Hook I/O and payload validation | `pydantic` 2.14 | Typed models for hook input (`prompt_id`, `transcript_path`, `last_assistant_message`) and for the classifier's output; one schema serves the API call, the Ollama call and the eval runner | `dataclasses` + hand-written checks |
| Task store on disk | `filelock` 4.2 + `os.replace` | Per-session lock file, context manager, cross-platform; `tmp + os.replace` gives the atomic write the plan asks for | `portalocker` 4.4 (adds PID-file locks for stale-lock detection) |
| Dependency order and cycle check | `graphlib.TopologicalSorter` (stdlib) | `static_order()` for the DAG, `prepare()` raises `CycleError` naming the cycle; `get_ready()`/`done()` map directly onto "pick the next ready task" | `networkx` only if we ever need graph algorithms beyond ordering |
| Classifier, hosted | `anthropic` 1.15 | `client.messages.parse(output_format=Model)` returns a validated Pydantic object; `with_options(timeout=2.0, max_retries=0)` gives the fail-open budget | raw `httpx` |
| Classifier, local | `ollama` 0.6 | `chat(format=Model.model_json_schema())` constrains the output to our schema; same Pydantic model as above | `llama-cpp-python` |
| Amend target matching, baseline | `rapidfuzz` 3.14 | C++ token-set ratios, MIT licence; replaces the word-overlap code that misfired in the plan's §4, and is the cheap first pass before a model call | `difflib.SequenceMatcher` (stdlib, slower, no token scorers) |
| Amend target matching, semantic | `model2vec` 0.10 | Static embeddings, numpy-only dependency, ~8–30 MB model, fast enough to run inside the hook; cosine similarity between the message and each queued task's intent | `sentence-transformers` (needs torch; too heavy for a hook) |
| Transcript tail | stdlib (`os.SEEK_END` + `json`) | Read the last N KB, split lines, parse the last assistant entry; no library needed | `jsonlines` 4.0 for the writer side if we want it |
| Decision log | stdlib `json` + `open(..., "a")` | One JSON object per line, appended under the same lock as the store | `orjson` 3.13 if logging ever shows up in the latency profile |
| Eval runner | `pydantic-evals` 2.57 | `Dataset`/`Case` map one-to-one onto the golden set, `EqualsExpected` covers label matching, custom evaluators cover `also_acceptable` and the asymmetric dependency score, metadata filtering slices by `difficulty` and `context` | `pytest` parametrised over the JSONL (simpler, no reporting) |
| Isolated model call from a hook | `claude-agent-sdk` 0.2 | `query()` with `system_prompt=<ours>`, `setting_sources=[]`, `max_turns=1`; the Python form of the `claude -p --setting-sources ""` isolation the plan requires. Still spawns the CLI, so detached use only | `subprocess` + `claude -p` flags |
| CLI (`queue list`, `promote`, `cancel`) | `typer` 0.27 | Type-hint driven, small | `argparse` (stdlib) |

## Notes per area

### Hook plumbing

The official hooks reference (checked 2026-10-18) settles three things the plan left open or got from the reference projects:

- `prompt_id` is a documented common input field: "UUID identifying the user prompt currently being processed". The mid-turn test is official, not reverse-engineered.
- `Stop` and `SubagentStop` input carries `last_assistant_message`. The docs say hooks that need the final assistant text should use it "instead of reading the transcript", because the transcript file "is written asynchronously and may lag". So the transcript-tail reader is only needed for *earlier* context (the `recent` field in the golden set), not for the closing message.
- Timeouts: command hooks default to 600 s, lowered to 30 s on `UserPromptSubmit`. The "Stop hook has 5 s" constraint quoted from claude-task-manager is not in the current docs. The 2 s classifier budget stays, because the user is waiting, but a detached judge is a choice, not a necessity.
- `continue: false` is a universal output field that "takes precedence over any event-specific decision fields", with `stopReason` shown to the user and kept in the conversation. On Pre/PostToolUse the stop applies even if the tool call fails.
- `Stop` accepts `hookSpecificOutput.additionalContext` for "non-error feedback that continues the conversation". That is the handover path the plan chose.
- `UserPromptSubmit` cannot replace the prompt; it can block it (exit 2 or `decision: "block"`) or add `additionalContext` alongside it.

No library needed; `pydantic` models for the payloads keep the three hook scripts honest.

### Task store

```python
from filelock import FileLock
import json, os, tempfile

def write_store(path, data):
    with FileLock(path + ".lock", timeout=5):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
```

`filelock` locks are advisory on POSIX, which is fine: every writer is our own code. `portalocker` is the swap if we want a PID-file lock to detect a hook that died holding the lock.

### Dependency order

```python
from graphlib import TopologicalSorter, CycleError

def ready_tasks(tasks):            # tasks: {id: {"depends_on": [...], "status": ...}}
    ts = TopologicalSorter({t: set(v["depends_on"]) for t, v in tasks.items()})
    try:
        ts.prepare()               # raises CycleError on an amend that creates a loop
    except CycleError as e:
        raise ValueError(f"dependency cycle: {e.args[1]}")
    for t in tasks:
        if tasks[t]["status"] == "done":
            ts.done(t)
    return [t for t in ts.get_ready() if tasks[t]["status"] not in ("done", "cancelled", "parked")]
```

Run the cycle check on every `amend` that rewires `depends_on` (g27) before writing the store.

### Classifier

One Pydantic model, three backends. The model is the contract the plan's M1 asks for:

```python
from pydantic import BaseModel, Field
from typing import Literal

class Part(BaseModel):
    label: Literal["steer", "queue", "amend", "split", "unclear"]
    interrupt: bool = False
    explicit_stop: float = Field(0, ge=0, le=1)   # second signal for the interrupt rule
    parked: bool = False
    depends_on: list[str] = []
    target: str | None = None                      # amend
    note: Literal["approve", "revise plan", "cancel", "pause"] | None = None
    confidence: float = Field(..., ge=0, le=1)

class Decision(BaseModel):
    label: Literal["steer", "queue", "amend", "split", "unclear"]
    parts: list[Part] = []                         # split only
    part: Part | None = None                       # everything else
```

Hosted (D2 option a). The plan names Haiku for the inline call because of the latency budget; Opus 5.5 is the default elsewhere and the right pick for the done judge, where latency does not matter:

```python
import anthropic
client = anthropic.Anthropic().with_options(timeout=2.0, max_retries=0)
try:
    r = client.messages.parse(
        model="claude-haiku-5-5",
        max_tokens=512,
        system=CLASSIFIER_PROMPT,
        messages=[{"role": "user", "content": payload_json}],
        output_format=Decision,
    )
    decision = r.parsed_output
except (anthropic.APITimeoutError, anthropic.APIError):
    decision = None                                # fail open: steer, log classifier=null
```

Local (D2 option b), same schema:

```python
import ollama
r = ollama.chat(model="qwen3:8b", messages=[...], format=Decision.model_json_schema(),
                options={"temperature": 0})
decision = Decision.model_validate_json(r.message.content)
```

Shadow mode can run both and log disagreements, which is the cheapest way to settle D2.

### Amend target matching

Two stages, cheap first:

1. `rapidfuzz.process.extractOne(message, {id: intent for queued tasks}, scorer=fuzz.token_set_ratio)`. Accept only above a high threshold (around 85) and only when the runner-up is well below; otherwise fall through.
2. `model2vec` cosine similarity between the message and each intent. The model loads once per hook invocation (~8 MB, tens of milliseconds); encoding a handful of strings is sub-millisecond.
3. If neither stage separates the candidates, hand the candidate list to the classifier and let it pick `target`.

Cases g12, g14, g16, g23 and g43 are the acceptance tests for this; g12 is the one the baseline folds wrongly.

### Eval runner

`pydantic-evals` gives reporting for free; the custom evaluator carries the asymmetric dependency rule:

```python
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

class IntakeScore(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> float:
        got, exp = ctx.output, ctx.expected_output
        acceptable = [exp] + ctx.metadata.get("also_acceptable", [])
        if any(matches(got, a) for a in acceptable):
            return 1.0
        if missing_hard_dependency(got, exp):      # task would run before something it needs
            return -1.0                            # hard fail: the M1 gate requires zero of these
        return 0.0
```

Load the golden set with `Case(name=row["id"], inputs=row, expected_output=row["expected"], metadata={...})`. The gate from the spec §8 is then: no `-1.0` scores, and all cases with `interrupt: true` score `1.0`.

If that feels like too much machinery for M1, `pytest` with `@pytest.mark.parametrize` over the JSONL does the same without reports, and can be swapped later.

### Isolated model call

The plan's rule is that any model call from a hook must not see the project's settings, MCP servers or dynamic system prompt, or it may answer the message instead of classifying it. For the classifier we avoid the problem entirely by calling the API directly. For anything that needs Claude Code's own tools (a planner that reads the repo to write `plans/pN.md`), `claude-agent-sdk` is the Python form:

```python
from claude_agent_sdk import query, ClaudeAgentOptions
opts = ClaudeAgentOptions(system_prompt=PLANNER_PROMPT, setting_sources=[],
                          max_turns=8, model="opus", cwd=project_dir)
async for msg in query(prompt=handoff_text, options=opts):
    ...
```

`setting_sources=[]` skips user, project and local settings. It still launches the CLI, so this belongs in the detached planner, never on the inline path.

## Chosen classifier caller: `claude -p`

```python
import json, subprocess

def classify(payload: dict, schema: dict, system: str, timeout=15) -> dict | None:
    cmd = ["claude", "-p", "--model", "haiku", "--setting-sources", "", "--strict-mcp-config",
           "--tools", "", "--output-format", "json", "--json-schema", json.dumps(schema),
           "--system-prompt", system, json.dumps(payload)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout
        return json.loads(out).get("structured_output")      # None on any miss: fail open
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError):
        return None
```

Measured 3.6 to 5.5 s wall-clock; see [decisions.md](decisions.md) D2b. Validate `structured_output` with the Pydantic `Decision` model before acting on it.

## Mod route (not chosen, kept as the upgrade path)

A mod is a plugin with an in-process JavaScript hooks module. It would replace most of this page: `prompt.submit` carries `turnId` (mid-turn flag) and `wait`; `$.model.classify` and `$.model.complete` call the model on the session's auth with no CLI start-up; `$.turn.abort` interrupts immediately; `$.prompt.submit` starts the next task's turn when idle; `turn.complete` gives the closing text and `isAborted` (Esc); `$.store` is a per-plugin JSON store; a `Pane` render site draws the queue. Early-access surface. Reference: `mods/types/claude-code.d.ts` in the claude-code repository, and https://code.claude.com/docs/en/plugins/mods/reference.

## Not recommended

- `sentence-transformers`, `torch`: tens of seconds of import time; wrong tool for a hook.
- `networkx`: `graphlib` covers ordering and cycle detection.
- `watchdog`, `sqlite3` for the store: the store is one small JSON file per session; a lock and an atomic rename are enough.
- `instructor`: the `anthropic` SDK's `parse()` and Ollama's `format=` already do schema-constrained output.

## Sources

- Claude Code hooks reference: https://code.claude.com/docs/en/hooks
- Claude Agent SDK, Python: https://code.claude.com/docs/en/agent-sdk/python
- Anthropic Python SDK on PyPI: https://pypi.org/project/anthropic/
- `graphlib`: https://docs.python.org/3/library/graphlib.html
- `filelock`: https://pypi.org/project/filelock/ · `portalocker`: https://portalocker.readthedocs.io/en/latest/
- `rapidfuzz`: https://pypi.org/project/RapidFuzz
- `model2vec`: https://pypi.org/project/model2vec
- Ollama structured outputs: https://registry.ollama.ai/blog/structured-outputs
- Pydantic Evals: https://ai.pydantic.dev/evals/evaluators/overview
