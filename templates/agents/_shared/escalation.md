# Escalation Guide

Escalation means applying `status:blocked` to the issue and posting a comment
that gives a human everything they need to unblock you.

## When to Escalate

Escalate when you are stuck on something you genuinely cannot resolve:

- Missing credentials, secrets, or access that only a human can provide
- Ambiguous requirements where any interpretation could be wrong
- A broken dependency, external service outage, or infrastructure problem
- A merge conflict that cannot be resolved without human design input
- You have retried twice and the same failure recurs

## When NOT to Escalate

Do not escalate things you can solve yourself:

- A test failure you introduced — fix it
- A linting error — fix it
- Uncertainty about which file to edit — read the codebase and decide
- A missing import or dependency that is clearly correct to add
- Normal tool or API transient errors on the first attempt — retry once

Escalating avoidable blockers wastes human attention and slows the board.

## How to Write a Clear Escalation Comment

Post a comment on the issue before applying the label. The comment must include:

1. What you were trying to do (one sentence)
2. What went wrong (the exact error, missing value, or ambiguity)
3. What you already tried
4. What a human needs to do to unblock you (be specific)

Example:

> **Blocked**
> Trying to run the integration tests for the payment service.
> The tests require a `STRIPE_TEST_KEY` secret that is not set in this
> repository's Actions secrets. I tried using the sandbox key from the
> README but it has been revoked.
> A human needs to add a valid `STRIPE_TEST_KEY` to the repo's Actions
> secrets (Settings > Secrets > Actions > New repository secret).

## After Escalating

- Apply `status:blocked` to the issue
- Exit cleanly (exit 0) — do not loop or retry after escalating
- Do not remove the `status:blocked` label yourself; wait for a human to
  resolve the blocker and re-trigger the workflow
