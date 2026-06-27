# Loop Guardrails

Infinite loops are the most dangerous failure mode in an event-driven agent
system. A label loop can trigger hundreds of workflow runs in minutes.

## The Core Rule

Never re-apply the label you just removed from an issue.

If you are the builder and you just removed `status:todo`, do not re-apply
`status:todo`. If you are the reviewer and you just removed `status:in-review`,
do not re-apply `status:in-review`.

## Label Transition Rules

Each role owns specific transitions. Stay in your lane:

- Builder: removes status:todo or status:changes-requested; applies status:in-review
- Reviewer: removes status:in-review; applies status:approved or status:changes-requested
- Docs: removes status:approved; applies status:done
- Planner: removes status:planning; applies status:todo

Applying a label outside your role's allowed output set is a protocol violation.

## Retry Counter

Before making any change that could loop, check whether you have already
attempted this work:

1. Read the issue comments. Count how many run-receipt comments exist from
   the same role (e.g., "builder run receipt").
2. If you see two or more prior receipts for the same status transition, you
   are in a retry loop.
3. On the third attempt: do not retry. Apply `status:blocked` and explain in
   a comment which transition has been attempted N times and what fails each time.

## AGENT_MAX_TURNS

The `AGENT_MAX_TURNS` environment variable caps how many turns your runner
will execute. When you are approaching the turn limit:

- Do not attempt a large new task.
- Post a progress comment summarising what you completed and what remains.
- Apply `status:blocked` before the runner hard-stops you.

Exiting cleanly (exit 0) with `status:blocked` applied is always better than
being killed mid-turn with no label change.

## Detecting You Are Stuck

Signs that you are in a loop or stuck state:

- You have attempted the same file edit more than twice and it keeps failing
- The test suite fails on the same assertion every run with no change
- You are circling between reading the same two files without making progress
- You cannot determine which file to change after reading three different ones

If any of these apply: stop, post a comment, apply `status:blocked`, exit 0.

## Summary

When in doubt: escalate rather than retry. A human unblocking one issue is
always cheaper than a runaway loop consuming Actions minutes and API credits.
