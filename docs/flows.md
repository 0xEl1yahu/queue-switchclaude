# Flows and core logic

Diagrams for how the harness behaves. GitHub renders the Mermaid blocks inline. The design itself is in [design-spec.md](design-spec.md); the cases that pin down each branch are in [`eval/golden_queue_intake.jsonl`](../eval/golden_queue_intake.jsonl).

## 1. What the user experiences

The user keeps typing while Claude works. Each message lands in one of five places, and the harness tells them which.

```mermaid
flowchart LR
    U([User types a message<br/>while Claude is working]) --> I{Intake}
    I -- steer --> S[Goes to Claude now,<br/>as part of the running turn]
    I -- interrupt --> X[Claude stops at the<br/>next tool call]
    I -- queue --> Q[(Queue)]
    I -- amend --> Q
    I -- parked --> P[(Parked list)]
    Q --> D{Dispatch<br/>when Claude stops}
    D --> PL[Plan written<br/>plans/pN.md]
    PL --> A{User approves?}
    A -- lgtm --> R[Task runs]
    A -- revise --> PL
    R --> H[Handoff written<br/>handoffs/pN.md]
    H --> D
    P -. promoted by the user .-> Q
```

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
        INTAKE[intake.py<br/>filter, detect mid-turn, classify]
        INTR[interrupt.py<br/>check flag]
        DISP[dispatch.py<br/>check done, handoff, plan next, approve]
        CLS[[Classifier<br/>API call, 2 s timeout, fails open]]
        JUDGE[[Done judge<br/>detached, slow is fine]]
        STORE[(Task store<br/>per-session JSON, file lock, atomic write)]
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
    DISP --> LOG
    INTAKE -- "steer: let it through" --> UPS
    INTAKE -- "queue/amend/interrupt: block" --> UPS
    INTR -- "continue:false" --> PTU
    DISP -- "additionalContext = next task" --> STP
```

## 3. Intake: how a message is classified

The rules that run before the model are cheap and deterministic. The model only sees what survives them.

```mermaid
flowchart TD
    M([Message arrives<br/>UserPromptSubmit]) --> T{Harness traffic?<br/>system reminder,<br/>agent report}
    T -- yes --> PASS[Pass through<br/>no log entry]
    T -- no --> N{Non-task?<br/>ok, yes, continue,<br/>slash command, lone dot}
    N -- yes --> PASS
    N -- no --> MT{prompt_id equals<br/>running turn's id?}
    MT -- "no: new turn" --> ST{Harness state}
    MT -- "yes: mid-turn" --> ST
    ST --> CTX[Build context:<br/>state, current task, queue,<br/>last assistant text from transcript tail]
    CTX --> PG{Paste guard?<br/>150+ words or 12+ lines}
    PG -- yes --> ONE[Treat as one ask,<br/>never split]
    PG -- no --> CL
    ONE --> CL[[Classifier call<br/>2 s timeout]]
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
    end
```

## 5. Task lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: intake classifies queue
    [*] --> parked: intake classifies queue + parked
    parked --> queued: user promotes it
    queued --> ready: every depends_on is done
    ready --> planned: dispatch writes plans/pN.md
    planned --> awaiting_approval: approval gate on (D3)
    planned --> running: approval gate off (D3)
    awaiting_approval --> running: user approves
    awaiting_approval --> planned: user revises
    running --> done: verify command passes, or judge says done (D4)
    running --> paused: user says do something else first (g27) or presses Esc (D5)
    paused --> ready: user resumes
    queued --> cancelled: amend with note cancel (g16, g43)
    ready --> cancelled: amend with note cancel
    done --> [*]
    cancelled --> [*]

    note right of queued
        amend can change text or
        rewire depends_on here;
        a stale child plan is re-planned
    end note
```

## 6. Dispatch: what happens each time Claude stops

```mermaid
sequenceDiagram
    autonumber
    participant C as Claude Code
    participant S as Stop hook (dispatch.py)
    participant J as Done judge (detached)
    participant T as Task store
    participant U as User

    C->>S: Stop event (transcript_path)
    S->>S: read last assistant text from transcript tail
    S->>T: file closing message against running task
    alt task has a verify command
        S->>S: run it
    else no verify command
        S-->>J: spawn judge (closing msg + next user msg)
        J-->>T: mark done / not done later
    end
    S->>T: write handoffs/pN.md
    S->>T: pick next task with all depends_on done
    alt nothing ready
        S-->>C: return, no additionalContext
    else task ready
        S->>S: write plans/pN+1.md from the handoff note
        S->>S: spin guard: same queue as last stop? then return
        opt approval gate on
            S-->>U: plan shown, state = awaiting_approval
            U-->>S: lgtm / revise (next turn, via intake)
        end
        S-->>C: hookSpecificOutput.additionalContext = next task + plan
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

Milestone M0 runs the whole intake path but acts on nothing. Every decision goes to the log; messages pass through untouched. Flip one flag to go active.

```mermaid
flowchart LR
    M([Message]) --> IN[Intake pipeline<br/>filter, context, classify]
    IN --> L[(Decision log)]
    IN --> G{mode}
    G -- shadow --> PASS[Pass through,<br/>always]
    G -- active --> ACT[Block / store / flag<br/>per class]
    L -. harvested into .-> GS[(golden set)]
```
