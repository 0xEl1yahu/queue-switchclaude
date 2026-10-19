# M1 eval: the golden set through the harness

Two records live here. The first section is the M1 gate (Task 9): the numbers the milestone is judged on, what was changed to reach them, and what is left. The second is the first full run from Task 8, made before the v2 classifier contract had merged, kept as the baseline.

## Gate result (Task 9)

Run on 2026-10-19 from the Task 9 worktree (branch `worktree-wf_b008e350-354-12`, main at `77bb51b` with Tasks 1 to 8 merged) with Claude Code 2.1.302 and the `haiku` alias. Three full model runs out of the four the plan allows.

| run | command | passed | score | missed hard dep | interrupts | rules alone | gate |
|---|---|---|---|---|---|---|---|
| rules only | `python3 eval/run_golden.py --rules-only` | 24 / 47 | +24.0 | 0 | 2 / 2 | 7 / 47 (15%) | PASS |
| 1, v2 prompt, cold cache | `python3 eval/run_golden.py --report eval/reports/m1-run1.md` | 44 / 47 | +44.0 | 0 | 2 / 2 | 7 / 47 (15%) | PASS |
| 2, same prompt, `--no-cache` | `python3 eval/run_golden.py --no-cache --report eval/reports/m1-run2.md` | 44 / 47 | +44.0 | 0 | 2 / 2 | 7 / 47 (15%) | PASS |
| 3, gate, after the `match.py` change | `python3 eval/run_golden.py --report eval/reports/m1-gate.md` | 44 / 47 | +44.0 | 0 | 2 / 2 | 7 / 47 (15%) | PASS (exit 0) |

Runs 1 and 2 each made 40 `claude -p` calls (mean 4.1 s and 3.9 s, about 2 min 45 s wall-clock, roughly $0.02 each). Run 3 made 7 fresh calls (g10 to g16: the `match.py` change altered the candidate scores in their payloads, so their cache keys changed) and took the other 33 model answers from run 2's cache; the remaining 7 cases are rule-decided and never reach the model. The reports are under `eval/reports/` (gitignored, like `eval/cache/`).

### Totals of the gate run

| | cases | passed | score |
|---|---|---|---|
| all | 47 | 44 | +44.0 |
| steer | 24 | 23 | |
| queue | 14 | 12 | |
| split | 5 | 5 | |
| amend | 4 | 4 | |
| easy | 12 | 11 | |
| medium | 18 | 17 | |
| hard | 17 | 16 | |

| gate input | value |
|---|---|
| `missed_hard_dependency` | 0 |
| interrupt cases correct (g29, g30) | 2 / 2 |
| classifier errors (fail-open steer) | 0 |
| decided by rules alone | 7 / 47 (15%): g03, g05, g06, g29, g30, g33, g35 |
| gate | **PASS (exit 0)** |

### Failing cases

The same three cases fail in every run, and none of them is a model miss: every case the model decided scored 1.0 in all three runs.

- g03 `stop`: the `interrupt` rule fires and the harness answers `steer` + `interrupt: true`; the golden `expected` is a bare `{"label": "steer"}`. Disputed, see below. Left failing.
- g08 `can you also fix the typo in the README`: the model answers `queue`, 0.9, `depends_on: []`, which is the expected answer. `intake._queue` treats an empty `depends_on` as "chain to the last queued task, else the running task" and writes `["p1"]`; the runner mirrors intake on purpose (`test_decide_model_empty_depends_on_falls_back_to_chain_like_intake`). Scored `dependency_extra` (0.0). See "Remainder".
- g25 `bump fastapi to the latest version`: the model answers `queue`, 0.9, `depends_on: []`, again the expected answer. The chain fallback writes `["p2"]` (the queued webhook handler), which neither `expected` (`[]`) nor `also_acceptable` (`["p1"]`) allows. Scored `dependency_extra` (0.0). See "Remainder".

### What changed in Task 9

No case failed on the prompt, the rules or target matching, so the plan's step 1 (classify each miss as rule, target or model) found nothing to fix in those files for the golden set itself. One latent target miss showed up in the candidate rankings the runner feeds the model, and was fixed:

- `scripts/qsc/match.py`: the subject view now weighs each subject word by `1/n`, `n` being the number of candidate tasks whose subject carries it (`rank` computes the document frequency over the candidate list and passes it to `score`; a lone `score(message, task_text)` call is unchanged). Before the change, g15's message `add rate limiting to login` resolved to p3 `Write tests for /login` at 0.67 against p2 `Add a /login endpoint that returns a JWT` at 0.40, on the strength of the one word both tasks share. Had the model ever answered `amend` without a target for a message like that, intake would have annotated the tests task. After the change p3 scores 0.54 and `pick` abstains. The four golden amend cases still resolve the same way: g14 and g23 by text (p3 at 0.80, p2 at 1.00), g16 by text (p3 at 0.80) or by the model's target, g43 by the model's target only (`nvm, scrap that` names nothing; text matching abstains at 0.22). The 0.6 accept and 0.2 margin are unchanged.
- `tests/test_match.py`: the g15 regression, plus golden-set sweeps: every amend case resolves to its expected target or abstains, never to another task; no steer case with a running task resolves to a queued task; g14 and g23 resolve without the model.
- `tests/test_rules.py`: golden-set sweeps: every rule-decided case (g03, g05, g06, g29, g30, g33, g35) agrees with `expected` or `also_acceptable` (g03 on the label; its flag is the disputed point); every undecided case reaches the classifier with its text untouched.
- `prompts/intake_system.md`: unchanged. The Task 2 prompt already decided every model case correctly in three runs; rewording it to move g08 or g25 would mean asking the model for a worse answer (a dependency the golden set says is wrong) to satisfy a fallback that belongs in intake.

Whole suite after the change: `python3 -m pytest -q`, 302 passed (297 before Task 9).

### Stability

Runs 1 and 2 used the same prompt and payloads; run 2 bypassed the cache so every answer was fresh. Across the 40 model-decided cases the label, dependencies, target, note and parts were identical in both runs. Five confidences moved: g13 0.93 to 0.90, g21 0.92 to 0.90, g22 0.93 to 0.95, g38 0.70 to 0.60, g40 0.80 to 0.60. None crossed the 0.9 threshold.

Two caveats on what these numbers mean:

1. **The golden set is also the prompt's worked examples** (spec section 11: "the golden set's `rationale` fields are the prompt's worked examples"; 34 of the 47 cases appear in `prompts/intake_system.md` with their expected answer, and the other 13 are near relatives of those). The model's answers mirror those examples closely, down to the confidence values. The 44 / 47 therefore measures how faithfully haiku applies the prompt's rules to messages it has seen described, not how it generalises. A held-out set is the M2 question; until then the live acceptance (Task 11) is the generalisation check.
2. **Several queue cases sit exactly on the threshold.** g07, g08, g13, g18, g21, g25, g37 and g41 come back at confidence 0.90, the lowest value intake acts on. A run in which the model says 0.85 on one of them scores that case 0 (`label`: the harness says `steer`). That cannot fail the gate: a steer never misses a hard dependency, and both interrupt cases are rule-decided. It can lower the pass count. The examples for those cases say 0.9 deliberately (the plan's "two signals" and "prefer steer when unsure" rules); raising them to buy headroom would be calibrating the model to the eval rather than to the messages.

### Disputed cases

- **g03** `stop`: `expected` is `{"label": "steer"}` with no `interrupt` flag, while the rationale says "Interrupt. Must never be queued." and g29/g30 carry `interrupt: true` for the same intent. The harness treats `stop` as an interrupt (M0 acceptance scenario B) and the scoring compares the flag both ways, so g03 scores 0 with reason `interrupt`. The golden set is read-only, so it stays failing. The likely fix is `interrupt: true` on g03, which would also make it a third gate case; that is an edit to the golden set and needs a decision outside this task.

### Remainder

- **The chain fallback overrides a deliberate empty `depends_on`** (g08, g25; first noted by Task 8 below). `intake._queue` does `deps = depends_on or _chain_deps(s)`, so the model has no way to say "this task depends on nothing": every independent task waits for whatever was queued last, and on g25 that is the wrong task. The fix is in `scripts/qsc/intake.py` and `eval/run_golden.py` together, both outside Task 9's files: apply the chain only when the decision came from a rule (`queue_marker`, where there is no model answer to trust) and take the model's `depends_on` verbatim, `[]` included, when the model decided. With that change both cases score 1.0 on the cached answers (the model already says `[]`), taking the set to 46 / 47 with only g03 outstanding. `tests/test_run_golden.py::test_decide_model_empty_depends_on_falls_back_to_chain_like_intake` pins the current behaviour and would be inverted by the same change.

## First full run (Task 8, v1 contract)

Run on 2026-10-19 from the Task 8 worktree (branch `worktree-wf_b008e350-354-10`, M0 code plus the eval runner) with Claude Code 2.1.302 and the `haiku` alias.

```
python3 eval/run_golden.py --report eval/reports/m1-first.md
```

47 cases, 41 model calls (`claude -p`, mean 4.0 s each), 2 min 45 s wall-clock. Model answers are cached under `eval/cache/` (gitignored); a cached re-run takes 0.1 s.

**Contract caveat.** That worktree held the M0 classifier contract (`classify` v1: labels `steer | queue | interrupt | unclear`, no `amend`, `split`, `parked` or `note`) and the v1 prompt, because Tasks 2 to 4 had not merged when the run was made. The 13 cases whose expected label or note cannot be expressed in v1 (4 amend, 5 split, g33 parked, g44/g45/g47 steer notes) therefore failed on `label` or `note` no matter what the model said. The runner is written against both contracts; the gate section above is the re-run after the merge.

### Totals

| | cases | passed | score |
|---|---|---|---|
| all | 47 | 30 | +30.0 |
| steer | 24 | 20 | +20.0 |
| queue | 14 | 10 | +10.0 |
| split | 5 | 0 | +0.0 |
| amend | 4 | 0 | +0.0 |
| easy | 12 | 10 | +10.0 |
| medium | 18 | 12 | +12.0 |
| hard | 17 | 8 | +8.0 |

| gate input | value |
|---|---|
| `missed_hard_dependency` | 0 |
| interrupt cases correct (g29, g30) | 2 / 2 |
| classifier errors (fail-open steer) | 0 |
| decided by rules alone | 6 / 47 (13%) |
| gate | **PASS** (exit 0) |

Rules-only baseline (`--rules-only`): 23/47 passed, +23.0, same 6/47 rule share, gate PASS. The rules decided g03, g05, g06, g29, g30 and g35; everything else reached the model. (After Task 4's `parked_marker` merged, the rules also decide g33: 7/47, 24 passed rules-only.)

The gate passed because the two conditions it checks (no missed hard dependency, both interrupt cases right) were met; 17 cases still scored 0.

### Failing cases

One line each; "model" is the raw cached answer.

- g03 `stop`: the `interrupt` rule fires, so the harness says `steer` + `interrupt: true`; the golden `expected` is a bare `{"label": "steer"}` although the rationale reads "Interrupt. Must never be queued." See disputed cases.
- g08 `can you also fix the typo in the README`: model `queue` 0.93 with `depends_on: []` (correct); intake's M0 chain fallback (empty `depends_on` means "depend on the chain") rewrote it to `["p1"]`, scored `dependency_extra`.
- g09 `use argon2 instead, and after that add password reset`: v1 has no `split`; model `unclear` 0.5, so `steer`.
- g14 `for the tests, include a rate-limit case`: v1 has no `amend`; model `steer` 0.6.
- g16 `skip the tests for now, I'll write them myself`: v1 has no `amend`; model `steer` 0.9.
- g20 `1) login endpoint 2) logout endpoint 3) tests for both`: v1 has no `split`; model `queue` 0.93 with no deps, so one task p2 with `depends_on: []` (idle, nothing to chain to).
- g23 `the webhook handler should verify Stripe signatures`: v1 has no `amend`; model `steer` 0.6.
- g24 `1. pull the tax calculation into its own module 2. add unit tests for it`: v1 has no `split`; model `steer` 0.85.
- g25 `bump fastapi to the latest version`: model `queue` 0.9 with `depends_on: []` (the expected answer); the chain fallback made it `["p2"]`, the last queued task, which neither `expected` nor `also_acceptable` (`["p1"]`) allows.
- g27 `actually do the webhook first, refactor can wait`: v1 has no `split`/`amend`; model `steer` 0.8.
- g33 `maybe later we can add social login`: v1 has no `parked` flag and no `parked_marker` rule (Task 4); model `unclear` 0.5.
- g41 `same for logout`: model `queue` 0.8 with `depends_on: ["p2"]` (right answer, under the 0.9 threshold), so `steer`.
- g43 `nvm, scrap that` (after `later: email verification`): v1 has no `amend`; model `steer` 0.85.
- g44 `lgtm` (awaiting approval): v1 has no `note`; model `steer` 0.95, expected `note: approve`.
- g45 `looks good but do the rate limiter before the endpoint`: v1 has no `note`; model `steer` 0.7, expected `note: revise plan`.
- g46 `go, and once it's in add logout`: v1 has no `split`; model `steer` 0.6.
- g47 `yeah do that`: v1 has no `note`; model `steer` 0.85, expected `note: approve`.

### What the run said for Task 9

1. **The chain fallback overrides a deliberate empty `depends_on`.** `intake.handle` (M0, kept by Task 6's "M0 queue branch") takes the chain when the model's `depends_on` is `[]`. g08 and g25 show the model answering `[]` correctly and the harness adding a dependency anyway; on g25 it is the wrong one (`p2`, the queued webhook, not the running refactor). Both score 0, not -1, so the gate does not see it, but every "independent" task will wait on whatever was queued last. The runner mirrors intake here on purpose (`test_decide_model_empty_depends_on_falls_back_to_chain_like_intake`); change intake and the runner together. Still open after Task 9, see "Remainder" above.
2. **Confidence sat under 0.9 on the hard cases.** g14 (0.6), g23 (0.6), g41 (0.8), g45 (0.7), g46 (0.6): even where the v1 label was right (g41), the threshold dropped it to `steer`. Resolved by the v2 prompt's worked examples: all five score 1.0 in the gate runs.
3. **Thirteen cases needed the v2 contract** (amend, split, parked, note). Resolved by Tasks 2 to 4: all thirteen score 1.0 in the gate runs.
4. **No missed hard dependency anywhere**, including the four queue cases that reach the model with a real dependency (g12 `p2`, g13 `p3`, g17/g18 `p1`, g37 `p2`), and both interrupts are rule-decided. Unchanged.

### Scoring notes

- `scoring.outcome` ignores `confidence` and `id`; ids are assigned by the runner as "highest known id + 1", which reproduces the golden numbering in every context (the plan's `p{len(queue)+1}` is off by one whenever a task is running).
- `missed_hard_dependency` is -1.0; an extra dependency is 0.0 (`dependency_extra`). Split cases score per part in order; any part with a missed dependency makes the case -1.0.
- The gate also fails when `claude` is missing or any classifier call fails (fail-open `steer` must not count as a pass), which is how `--cases g01` with no `claude` on `PATH` exits 1 without a traceback (review focus 5).
