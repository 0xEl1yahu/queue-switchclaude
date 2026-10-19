# Flows and core logic

Diagrams for how the harness behaves. GitHub renders the Mermaid blocks inline. The design itself is in [design-spec.md](design-spec.md); the cases that pin down each branch are in [`eval/golden_queue_intake.jsonl`](../eval/golden_queue_intake.jsonl).

## 1. What the user experiences

The user keeps typing while Claude works. Each message lands in one of five places, and the harness tells them which. Plans and handoffs are files under the plugin data dir (`sessions/<session>/plans/pN.md`, `.../handoffs/pN.md`), written and read by the session's own Claude; the harness only tells it which file.

```mermaid
flowchart LR
    U([User types a message<br/>while Claude is working]) --> I{Intake}
    I -- steer --> S[Goes to Claude now,<br/>as part of the running turn]
    I -- interrupt --> X[Claude stops at the<br/>next tool call]
    I -- queue --> Q[(Queue)]
    I -- amend --> Q
    I -- parked --> P[(Parked list)]
    Q --> D{Dispatch<br/>when Claude stops}
    D --> PL[Claude writes plans/pN.md<br/>and presents it]
    PL --> A{User approves?}
    A -- "lgtm, ok, queue: approve" --> R[Task runs]
    A -- "looks good but X first" --> PL
    A -- "unrelated message" --> O[That runs as a normal turn,<br/>then the plan is shown again]
    O --> A
    R --> QN{Done?<br/>verify command or judge}
    QN -- "asked a question" --> AN[User answers,<br/>task resumes]
    AN --> R
    QN -- "not done, max 2" --> R
    QN -- done --> H[Handoff written<br/>handoffs/pN.md]
    H --> D
    P -. promoted by the user .-> Q
```

A task marked `auto` (`queue: auto pN`) still gets a plan and a handoff but skips the approval stop: the plan flows straight into execution.

## 2. Where the harness sits

Three Claude Code hooks, one store on disk, one classifier call.

```mermaid
flowchart TB
    subgraph CC[Claude Code]
        UPS[UserPromptSubmit hook]
        PTU[PreToolUse / PostToolUse hooks]
        STP[Stop hook]
    end

    subgraph H[Harness]
        INTAKE[intake.py<br/>filter, detect mid-turn, waiting states, classify]
        INTR[interrupt.py<br/>check flag]
        DISP[dispatch.py<br/>close running task, handoff,<br/>record plan, choose next]
        CLS[[Classifier<br/>claude -p haiku, 15 s, fails open as steer]]
        JUDGE[[Done judge<br/>claude -p haiku, inline, 15 s, fails open as done]]
        VERIFY[[Verify command<br/>shell, 120 s, failure handed back]]
        STORE[(Task store<br/>per-session JSON, file lock, atomic write)]
        FILES[(plans/pN.md, handoffs/pN.md<br/>under the data dir)]
        LOG[(Decision log<br/>JSONL, shadow mode source)]
    end

    UPS --> INTAKE
    PTU --> INTR
    STP --> DISP
    INTAKE --> CLS
    INTAKE --> STORE
    INTAKE --> LOG
    INTR --> STORE
    DISP --> STORE
    DISP --> JUDGE
    DISP --> VERIFY
    DISP --> FILES
    DISP --> LOG
    INTAKE -- "steer: let it through" --> UPS
    INTAKE -- "queue/amend/interrupt/control: block" --> UPS
    INTAKE -- "approve: let through + execution handover" --> UPS
    INTR -- "continue:false" --> PTU
    DISP -- "additionalContext = plan, re-present,<br/>execute, or fix-and-stop" --> STP
```

Both model calls go through one wrapper, `qsc/model.py`, with the same isolation flags (`--setting-sources "" --strict-mcp-config --tools ""`), one system prompt each (`prompts/intake_system.md`, `prompts/judge_system.md`) and one JSON schema each.

## 3. Intake: how a message is classified

The rules that run before the model are cheap and deterministic. The model only sees what survives them.

```mermaid
flowchart TD
    M([Message arrives<br/>UserPromptSubmit]) --> T{Harness traffic?<br/>system reminder,<br/>agent report}
    T -- yes --> PASS[Pass through<br/>no log entry]
    T -- no --> CM{Control?<br/>queue: list, cancel p2,<br/>approve, verify p2 ...}
    CM -- yes --> CB[Run the command,<br/>block with its reply]
    CM -- no --> N{Non-task?<br/>ok, yes, continue,<br/>slash command, lone dot}
    N -- yes --> WS{Plan or question<br/>waiting on the user?}
    WS -- no --> PASS
    WS -- yes --> WAIT[Waiting states,<br/>section 3b]
    N -- no --> WS2{Plan or question<br/>waiting on the user?}
    WS2 -- yes --> WAIT
    WS2 -- no --> MT{prompt_id equals<br/>running turn's id?}
    MT -- "no: new turn" --> ST{Harness state}
    MT -- "yes: mid-turn" --> ST
    ST --> CTX[Build context:<br/>state, current task, queue,<br/>last assistant text from transcript tail]
    CTX --> PG{Paste guard?<br/>150+ words or 12+ lines}
    PG -- yes --> ONE[Treat as one ask,<br/>never split]
    PG -- no --> RL{Rules decide?<br/>stop, pN:, later:,<br/>maybe later, numbered list, lone dot}
    ONE --> RL
    RL -- yes --> R
    RL -- no --> CL[[claude -p haiku<br/>--json-schema, 15 s timeout<br/>measured 3.6 to 5.5 s]]
    CL -- "error or timeout" --> FO[Fail open: steer<br/>log classifier=null]
    CL --> R{Result}
    R -- unclear --> FO
    R -- steer --> S[Let through]
    R -- "steer + interrupt" --> I[Block message,<br/>set interrupt flag]
    R -- queue --> QG{Fake-split guard,<br/>dependency inference,<br/>cycle check}
    R -- parked --> PK[Store with parked=true,<br/>never auto-dispatched]
    R -- amend --> AM[Resolve target task<br/>semantically; apply text,<br/>cancel, or rewire depends_on]
    R -- split --> SP[Classify each part<br/>with the rules above]
    QG --> ST2[Store task, block message,<br/>tell the user its id]
    AM --> ST2
    SP --> ST2
```

### 3b. Intake while the store waits on the user

Two store states park the session on the user: `awaiting_approval` (a plan for `pending_task` is on screen) and `awaiting_answer` (the running task stopped by asking something). Intake reads the state before the non-task pass-through, so "ok" approves a waiting plan but is still a non-task when nothing waits. Control messages win first in every state.

```mermaid
flowchart TD
    W([Message while waiting]) --> ST{Store state}
    ST -- awaiting_approval --> AP{Approval word?<br/>lgtm, ok, go, ship it,<br/>exact after normalising}
    AP -- yes --> RUN[Plan runs: task running,<br/>message passes through with<br/>the execution handover]
    AP -- no --> RW{Rule?}
    RW -- "stop" --> NOOP[Block: nothing is running,<br/>pN is waiting for approval]
    RW -- "later: x" --> QW[Queue after the pending task,<br/>keep waiting]
    RW -- "non-task: continue, ." --> PT[Pass through, keep waiting]
    RW -- none --> CLW[[classifier, state awaiting_approval]]
    CLW -- "steer: approve" --> RUN
    CLW -- "steer: revise plan" --> REV[Pass through with revise instruction,<br/>plan stays planned, keep waiting]
    CLW -- "split: approve + queue (g46)" --> SPL[Apply queue and amend parts,<br/>then approve or revise]
    SPL --> RUN
    CLW -- "queue / amend" --> QW
    CLW -- "anything else, or no answer" --> DEF[Deferred: a normal turn runs now,<br/>dispatch re-presents the plan after it]
    ST -- awaiting_answer --> CF{Confirmation?<br/>ok, or ok as first clause:<br/>ok, merge}
    CF -- yes --> CONF[Task done, verdict confirmed,<br/>handoff written, next task<br/>planned on this same message]
    CF -- no --> RA{Rule?}
    RA -- "stop" --> NOOP2[Block: nothing is running,<br/>pN is waiting for your answer]
    RA -- "later: x" --> QW
    RA -- none --> ANS[The answer: task resumes running,<br/>message passes through untouched,<br/>no classifier call]
```

Interrupt needs two signals, so a stray "stop" inside a longer sentence does not halt the run:

```mermaid
flowchart LR
    A[interrupt score >= 0.9] --> C{both?}
    B[explicit-stop score >= 0.9] --> C
    C -- yes --> I[interrupt]
    C -- no --> Q{queue score >= 0.9<br/>and stop score < 0.5?}
    Q -- yes --> QU[queue]
    Q -- no --> S[steer]
```

## 4. How state changes what a message means

The same words classify differently depending on what the harness is doing. Cases from the golden set:

```mermaid
flowchart LR
    subgraph running[state: running]
        r1["'use bcrypt' → steer (g01)"]
        r2["'later: email verification' → queue (g35)"]
        r3["'maybe later social login' → queue, parked (g33)"]
        r4["'skip the tests' → amend: cancel p3 (g16)"]
        r5["'don't do that' after an announced action → steer + interrupt (g29)"]
    end
    subgraph idle[state: idle]
        i1["'now add the login endpoint' → queue, runs at once (g17)"]
        i2["'the user model's missing a unique constraint' → queue, not steer: nothing is running (g18)"]
        i3["'what did you change in p1?' → steer: a question is answered now (g19)"]
    end
    subgraph approval[state: awaiting_approval]
        a1["'lgtm' → steer: approve (g44)"]
        a2["'looks good but do X first' → steer: revise plan, NOT approve (g45)"]
        a3["'go, and once it's in add logout' → split: approve + queue (g46)"]
        a4["'ok' → approve, but the same word while running or idle is a non-task"]
    end
    subgraph answer[state: awaiting_answer]
        n1["'use postgres' → the answer: task resumes running"]
        n2["'ok, merge' → confirmation: task done, next task planned (spec section 7)"]
        n3["'ok, but use postgres' → the answer, not a confirmation"]
    end
```

## 5. Task lifecycle

Two state machines, kept in one store: each task has a status, and the store as a whole has a state that says what the session is waiting on. "Ready" is not a stored status: a `queued` task whose every `depends_on` is `done` is ready (`Store.ready()`).

### 5a. Task status

```mermaid
stateDiagram-v2
    [*] --> queued: intake classifies queue
    [*] --> parked: intake classifies queue + parked
    [*] --> running: the prompt of a new turn
    parked --> queued: queue promote pN
    queued --> planned: ready, Claude wrote plans/pN.md (store planning)
    queued --> queued: ready, but no plan after two planning turns
    planned --> running: approved (lgtm, queue approve), or auto
    planned --> queued: a dependency finished after the plan (stale), plan file deleted
    running --> running: verify failed or not done, handed back, at most twice
    running --> done: verify ok, judge done, stopped, gave up, or user confirmed (D4)
    running --> paused: user says do something else first (g27)
    paused --> queued: queue resume pN or promote pN
    running --> interrupted: Esc detected on next prompt (D5)
    queued --> dropped: Esc detected (D5), notice lists them
    interrupted --> [*]
    dropped --> [*]
    queued --> cancelled: amend with note cancel (g16, g43), or queue cancel pN
    planned --> cancelled: queue cancel pN
    parked --> cancelled: queue cancel pN
    done --> [*]
    cancelled --> [*]

    note right of queued
        amend can annotate or
        rewire depends_on here;
        the notes travel into the plan
    end note
    note right of done
        verdict and closing message
        are filed on the task and in
        handoffs/pN.md
    end note
```

`failed_verify` is a status the store, `queue: promote` and `queue: cancel` accept, but nothing sets it yet: a failing verify command is handed back twice and then the task is closed as `done` with verdict `gave_up`.

### 5b. Store state

```mermaid
stateDiagram-v2
    [*] --> idle
    idle --> running: any new prompt (intake new turn)
    idle --> planning: Stop finds a ready task (choose_next)
    running --> running: handback, verify failed or not done
    running --> awaiting_answer: judge says asked_question
    running --> planning: task closed, next ready task handed over for planning
    running --> awaiting_approval: task closed, a planned task is ready again (re-present)
    running --> idle: task closed, nothing ready or queue paused
    planning --> awaiting_approval: plan file written, gate on (D3)
    planning --> running: plan file written, task is auto (execute now)
    planning --> planning: no plan file, asked once more
    planning --> idle: no plan file twice, task back to queued
    awaiting_approval --> running: approval (lgtm, ok, queue approve, classifier approve)
    awaiting_approval --> running: unrelated message, a normal turn (plan re-presented after it)
    awaiting_approval --> awaiting_approval: revise plan, later queue, stop, non-task
    awaiting_answer --> running: the answer (task resumes), or a new unrelated prompt
    awaiting_answer --> planning: confirmation (ok, merge), next task planned
    awaiting_answer --> idle: confirmation, nothing left to plan
```

## 6. Dispatch: what happens each time Claude stops

Three stages, one function each in `dispatch.py`: close the running task (`_close_running`), record the plan a planning turn wrote (`_after_planning`), choose what is next (`choose_next`). Which ones run depends on the store state. Every handover is `hookSpecificOutput.additionalContext`, which keeps the turn alive with that text; a Stop in `awaiting_approval` or `awaiting_answer` changes nothing.

```mermaid
sequenceDiagram
    autonumber
    participant C as Claude Code
    participant S as Stop hook (dispatch.py)
    participant D as done.py (verify or judge)
    participant T as Task store + files
    participant U as User

    C->>S: Stop event (last_assistant_message, cwd, stop_hook_active)
    alt store state running: stage A, close the running task
        S->>D: decide(task, event, plan excerpt)
        alt task has a verify command
            D->>D: run it in cwd, 120 s timeout
        else no verify command
            D->>D: claude -p haiku judge, 15 s, fails open as done
        end
        D-->>S: verdict
        alt verify_failed or not_done, fewer than 2 handbacks
            S-->>C: additionalContext = fix it / finish it, then stop
        else asked_question
            S->>T: state = awaiting_answer, task stays running
            S-->>C: return (the user answers next)
        else done, verify_ok, stopped, gave_up
            S->>T: task done, verdict and closing filed, handoffs/pN.md written
        end
    else store state planning: stage B, after a planning turn
        alt plans/pN.md exists
            S->>T: task planned, plan_based_on = done ids
            alt task is auto
                S-->>C: additionalContext = execute the plan at plans/pN.md
            else gate on (D3)
                S-->>U: state = awaiting_approval, the plan is on screen
                U-->>S: lgtm / revise / unrelated (next message, via intake)
            end
        else no plan file
            S-->>C: ask once more, then task back to queued, state idle
        end
    end
    opt stage D, choose what is next (after A closed a task, or from idle)
        S->>T: state idle; stale planned children back to queued, their plan files deleted
        alt queue paused or shadow mode
            S-->>C: return, nothing handed over
        else a planned task is ready again
            S->>T: state = awaiting_approval
            S-->>C: additionalContext = pN is planned at plans/pN.md, present it and stop
        else a queued task is ready
            S->>S: spin guard: same task as the last continuation? then return
            S->>T: state = planning, pending_task = pN
            S-->>C: additionalContext = read handoffs of depends_on, write plans/pN.md, present it, stop (or carry on if auto)
        else nothing ready
            S-->>C: return, no additionalContext
        end
    end
```

## 7. Interrupt: why it happens at a tool boundary

```mermaid
sequenceDiagram
    participant U as User
    participant I as UserPromptSubmit (intake)
    participant T as Task store
    participant C as Claude (running turn)
    participant P as PreToolUse (interrupt.py)

    U->>I: "stop that now" (mid-turn, same prompt_id)
    I->>I: interrupt >= 0.9 and explicit-stop >= 0.9
    I->>T: set interrupt flag
    I-->>C: message blocked, not injected
    C->>P: next tool call
    P->>T: flag set?
    P-->>C: continue:false
    Note over C: turn ends here. Stop hook does not fire.
    Note over C,P: A text-only turn has no tool call,<br/>so it cannot be hard-stopped (known limit).
```

## 8. Dependency inference, by example

Case g20: "1) login endpoint 2) logout endpoint 3) tests for both", while idle with p1 done.

```mermaid
flowchart LR
    p1[p1 User model<br/>done] --> p2[p2 login endpoint]
    p2 --> p3[p3 logout endpoint]
    p2 --> p4[p4 tests for both]
    p3 --> p4
```

The naive "depend on the previous task" rule gets this wrong (p4 would depend only on p3). Case g12 is the same trap in the other direction: "after login's done, add logout" depends on p2 (login), not on p3 (tests), even though p3 was queued last.

## 9. Shadow mode

Milestone M0 runs the whole intake path but acts on nothing. The classifier is `claude -p` (decision D2b), so M0 also measures how often the rules decide without it. Every decision goes to the log; messages pass through untouched. Flip one flag to go active.

```mermaid
flowchart LR
    M([Message]) --> IN[Intake pipeline<br/>filter, context, classify]
    IN --> L[(Decision log)]
    IN --> G{mode}
    G -- shadow --> PASS[Pass through,<br/>always]
    G -- active --> ACT[Block / store / flag<br/>per class]
    L -. harvested into .-> GS[(golden set)]
```
