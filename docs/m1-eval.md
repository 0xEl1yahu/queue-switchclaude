# M1 eval: first full run of the golden set

Run on 2026-10-19 from the Task 8 worktree (branch `worktree-wf_b008e350-354-10`, M0 code plus the eval runner) with Claude Code 2.1.302 and the `haiku` alias.

```
python3 eval/run_golden.py --report eval/reports/m1-first.md
```

47 cases, 41 model calls (`claude -p`, mean 4.0 s each), 2 min 45 s wall-clock. Model answers are cached under `eval/cache/` (gitignored); a cached re-run takes 0.1 s.

**Contract caveat.** This worktree holds the M0 classifier contract (`classify` v1: labels `steer | queue | interrupt | unclear`, no `amend`, `split`, `parked` or `note`) and the v1 prompt, because Tasks 2 to 4 had not merged when the run was made. The 13 cases whose expected label or note cannot be expressed in v1 (4 amend, 5 split, g33 parked, g44/g45/g47 steer notes) therefore fail on `label` or `note` no matter what the model says. The runner is written against both contracts; rerun it after the merge for the real M1 numbers.

## Totals

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

Rules-only baseline (`--rules-only`): 23/47 passed, +23.0, same 6/47 rule share, gate PASS. The rules decide g03, g05, g06, g29, g30 and g35; everything else reaches the model.

The gate passes because the two conditions it checks (no missed hard dependency, both interrupt cases right) are met; 17 cases still score 0. Task 9 closes those.

## Failing cases

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

## What the run says for Task 9

1. **The chain fallback overrides a deliberate empty `depends_on`.** `intake.handle` (M0, kept by Task 6's "M0 queue branch") takes the chain when the model's `depends_on` is `[]`. g08 and g25 show the model answering `[]` correctly and the harness adding a dependency anyway; on g25 it is the wrong one (`p2`, the queued webhook, not the running refactor). Both score 0, not -1, so the gate does not see it, but every "independent" task will wait on whatever was queued last. The runner mirrors intake here on purpose (`test_decide_model_empty_depends_on_falls_back_to_chain_like_intake`); change intake and the runner together.
2. **Confidence sits under 0.9 on the hard cases.** g14 (0.6), g23 (0.6), g41 (0.8), g45 (0.7), g46 (0.6): even where the v1 label is right (g41), the threshold drops it to `steer`. Worked examples in the v2 prompt should target these.
3. **Thirteen cases need the v2 contract** (amend, split, parked, note). Re-run after Tasks 2 to 4 merge before changing the prompt.
4. **No missed hard dependency anywhere**, including the four queue cases that reach the model with a real dependency (g12 `p2`, g13 `p3`, g17/g18 `p1`, g37 `p2`), and both interrupts are rule-decided.

## Disputed cases

- **g03** `stop`: `expected` is `{"label": "steer"}` with no `interrupt` flag, while the rationale says "Interrupt. Must never be queued." and g29/g30 carry `interrupt: true` for the same intent. The harness treats `stop` as an interrupt (M0 acceptance scenario B) and the scoring compares the flag both ways, so g03 scores 0 with reason `interrupt`. The golden set is read-only; left failing for Task 9 to decide (the likely fix is `interrupt: true` on g03, which would also make it a third gate case).

## Scoring notes

- `scoring.outcome` ignores `confidence` and `id`; ids are assigned by the runner as "highest known id + 1", which reproduces the golden numbering in every context (the plan's `p{len(queue)+1}` is off by one whenever a task is running).
- `missed_hard_dependency` is -1.0; an extra dependency is 0.0 (`dependency_extra`). Split cases score per part in order; any part with a missed dependency makes the case -1.0.
- The gate also fails when `claude` is missing or any classifier call fails (fail-open `steer` must not count as a pass), which is how `--cases g01` with no `claude` on `PATH` exits 1 without a traceback (review focus 5).
