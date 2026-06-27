# Telemetry

AgentOS records a structured run record for every agent invocation. This
record is written by the runner wrapper — not by the agent itself.

## What the Agent Must Do

Your job is simple: exit cleanly so the runner can capture the record.

- Exit 0 on successful completion
- Exit 1 on an unrecoverable error
- Exit 2 if you hit the AGENT_MAX_TURNS limit before finishing
- Post a run receipt comment on the issue or PR before exiting (see below)

Do not write telemetry records yourself. Do not try to append to JSONL files
directly. The runner wrapper handles all of that.

## Run Receipt Comment

Before exiting, post a comment on the issue with a brief summary:

- What you did
- Whether you succeeded or are blocked
- The exit code you are about to return

This comment is how humans (and the orchestrator) know what happened. It is
also the signal the runner uses to confirm the agent reached its natural end
rather than being killed.

## JSONL Schema

The runner writes one JSON object per line to the ops-metrics/ directory.
Each record includes:

```
run_id          GITHUB_RUN_ID
role            AGENT_ROLE
issue_number    ISSUE_NUMBER
repo            GITHUB_REPOSITORY
exit_code       0 | 1 | 2
started_at      ISO 8601 timestamp
finished_at     ISO 8601 timestamp
duration_s      integer seconds
outcome         success | error | max_turns | blocked
```

Records accumulate in ops-metrics/runs.jsonl (or a date-partitioned path
depending on your ops repo configuration). They are used for dashboards,
cost tracking, and loop detection.

## ops-metrics/ Directory

The ops-metrics/ directory lives in the ops repository (or the target repo
if no separate ops repo is configured). Do not modify files in ops-metrics/
manually; they are owned by the runner wrapper and the board agent.
