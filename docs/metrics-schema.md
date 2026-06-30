# Metrics Schema — JSONL Run Record v6

This document is the authoritative reference for the agentOS run record format,
version 6. Run records are the primary data source for understanding agent
performance, cost, and reliability over time.

---

## 1. Overview

### Two-event lifecycle

Every agent run produces exactly two records in the JSONL log:

1. The run record — written by the builder/reviewer/etc. workflow when the agent
   process exits. Contains execution metrics, cost estimates, and the agent's
   self-reported status.

2. The settlement record — written by the watcher workflow when the associated PR
   is merged or the issue is closed. Contains outcome, merge metadata, and the
   final cost figure based on actual GitHub billing data (where available).

Both records share a `run_key` that links them. Analytics scripts always join on
`run_key` to get the complete picture of a run.

### JSONL format

Records are stored as newline-delimited JSON (JSONL). Each line is a complete,
self-contained JSON object. There are no multi-line records. The schema version is
recorded in every record as `schema_version: 6`.

Example single line (line-wrapped here for readability):

    {"schema_version":6,"event":"run","run_key":"my-org/my-repo:issue-42:run:20260115T143022Z",
    "identity":{"repo":"my-org/my-repo","issue_number":42,"role":"builder",...},...}

### One file per month

Log files are stored at:

    .agentOS/logs/runs-YYYY-MM.jsonl

One file per calendar month (UTC). The watcher creates the file if it does not
exist. Old files are never modified after the month rolls over (append-only within
the month). This makes the files safe to archive and safe to process with streaming
tools (jq, awk, etc.) without loading the entire history into memory.

---

## 2. The run_key Format

### Structure

    run_key = "<repo>:<issue-ref>:<event>:<timestamp>"

    my-org/my-repo:issue-42:run:20260115T143022Z
    my-org/my-repo:issue-42:settlement:20260115T161500Z

Fields:
- `<repo>`: owner/repo, e.g. my-org/my-repo
- `<issue-ref>`: issue-<number>, e.g. issue-42
- `<event>`: `run` or `settlement`
- `<timestamp>`: ISO 8601 compact UTC, e.g. 20260115T143022Z

### Parsing

In Python:

    def parse_run_key(run_key: str) -> dict:
        parts = run_key.split(":")
        # parts[0]/parts[1] = repo (owner/repo contains one slash, not a colon)
        repo = parts[0]
        issue_ref = parts[1]
        event = parts[2]
        timestamp = parts[3]
        issue_number = int(issue_ref.replace("issue-", ""))
        return {
            "repo": repo,
            "issue_number": issue_number,
            "event": event,
            "timestamp": timestamp,
        }

Note: The repo field contains a forward slash but does NOT contain colons, so
splitting on ":" is unambiguous.

### Deduplication

The run_key is designed to be unique per run event. The timestamp component uses
the time the run record was written (agent exit time), not the time the workflow
was triggered. Millisecond precision is not included because run records are
typically seconds apart. In the extremely rare case of two runs exiting in the same
second for the same issue, a sequential counter suffix is appended:

    my-org/my-repo:issue-42:run:20260115T143022Z-1
    my-org/my-repo:issue-42:run:20260115T143022Z-2

To deduplicate a JSONL corpus, use the run_key as the primary key. The settlement
record for a given run shares the same repo+issue+timestamp prefix but has event=settlement.

---

## 3. Identity Block

The identity block identifies who ran what on which issue.

    "identity": {
      "schema_version":  6,                          // integer — schema version
      "run_key":         "my-org/my-repo:issue-42:run:20260115T143022Z",  // string
      "repo":            "my-org/my-repo",           // string — owner/repo
      "issue_number":    42,                         // integer
      "role":            "builder",                  // string enum: builder|reviewer|watcher|docs|planner
      "runner":          "hermes",                   // string — runtime.runner value
      "runner_version":  "2.4.1",                    // string — semver of the runner binary
      "model":           "claude-opus-4-5",          // string — LLM model identifier
      "workflow_run_id": 9876543210,                 // integer — GitHub Actions run ID
      "workflow_run_url":"https://github.com/...",   // string — URL to the Actions run
      "agent_branch":    "agent/issue-42-add-hello", // string — branch created by builder
      "pr_number":       null                        // integer|null — set if PR was opened
    }

Field notes:
- `schema_version` is repeated here for convenience; always equals 6.
- `runner_version` is the version of the hermes/claude/codex binary, not open-agentos-cli.
- `model` is the primary model used. If multiple models were used in one run (e.g.,
  a cheap model for tool calls and an expensive model for synthesis), this field
  records the primary model and the per_turn array in the cost block records the
  actual model per turn.
- `pr_number` is null if the run did not open a PR (e.g., a reviewer run that
  requested changes does not open a new PR).

---

## 4. Lifecycle Block

The lifecycle block records timing information.

    "lifecycle": {
      "triggered_at":    "2026-01-15T14:28:00Z",   // string ISO 8601 — label event time
      "started_at":      "2026-01-15T14:30:22Z",   // string ISO 8601 — runner process start
      "ended_at":        "2026-01-15T14:42:08Z",   // string ISO 8601 — runner process end
      "duration_seconds": 706,                      // integer — ended_at minus started_at
      "queue_seconds":    142,                      // integer — started_at minus triggered_at
      "timeout_hit":      false                     // boolean — true if runner was killed by timeout
    }

Field notes:
- `triggered_at` is the timestamp of the GitHub event (label applied) that caused
  the orchestrator to dispatch. It is read from the GitHub event payload.
- `queue_seconds` measures the time between label application and runner start,
  including GitHub Actions queue time and checkout time. High queue times (> 120s)
  indicate runner resource contention.
- `timeout_hit` is true when the runner was killed by the AGENTOS_TIMEOUT limit.
  A timeout is recorded as a clean exit with status=timeout (see Section 9).

---

## 5. Execution Block

The execution block records what the LLM actually did.

    "execution": {
      "turns":            14,                       // integer — total LLM turns
      "tool_calls":       37,                       // integer — total tool invocations
      "compaction_events": 1,                       // integer — context window truncations
      "compaction_turns": [10],                     // array<integer> — turns where compaction occurred
      "files_changed":    5,                        // integer — files touched in the final diff
      "lines_added":      142,                      // integer — lines added in final diff
      "lines_removed":    18,                       // integer — lines removed in final diff
      "commits":          3,                        // integer — commits pushed to branch
      "tests_run":        true,                     // boolean — did the runner invoke a test suite
      "tests_passed":     true                      // boolean — did tests pass on final commit
    }

Field notes:
- `turns` counts full request-response cycles with the LLM. A turn that calls
  tools and then synthesises a response counts as one turn.
- `tool_calls` is the total number of tool invocations across all turns (reads,
  writes, bash executions, etc.).
- `compaction_events` counts how many times the context window was truncated or
  summarised. High compaction counts (> 2) indicate the issue was too large for
  the configured max_turns/context window and may need to be decomposed.
- `tests_run` and `tests_passed` are best-effort: the runner sets these by inspecting
  its own tool call history. They may be false even if the builder ran tests
  informally (e.g., a bash tool call that happens to run pytest but isn't
  classified as a test invocation).

---

## 6. Cost Block

The cost block records token usage and cost estimates.

    "cost": {
      "input_tokens":       45230,                  // integer — total input tokens across all turns
      "output_tokens":      8140,                   // integer — total output tokens across all turns
      "cache_read_tokens":  12000,                  // integer — tokens served from prompt cache
      "cache_write_tokens": 8000,                   // integer — tokens written to prompt cache
      "total_cost_usd":     0.47,                   // float — cost reported by the LLM provider API
      "modeled_cost_usd":   0.49,                   // float — cost computed from model_rates.yml
      "currency":           "USD",                  // string — always USD in v6
      "per_turn": [
        {
          "turn":           1,
          "model":          "claude-opus-4-5",
          "input_tokens":   3200,
          "output_tokens":  410,
          "cost_usd":       0.033
        },
        ...
      ]
    }

### total_cost_usd vs modeled_cost_usd

`total_cost_usd` is the cost figure returned by the LLM provider's API response
(usage.cost or equivalent). Not all providers return this field. When the provider
does not return a cost figure, `total_cost_usd` is null.

`modeled_cost_usd` is computed independently by agentOS using the token counts and
the rates in .agentOS/model_rates.yml. It is always present (never null) because
token counts are always available. See Section 12 for how it is computed.

When `total_cost_usd` is available, trust it over `modeled_cost_usd` for billing
purposes. When it is null, use `modeled_cost_usd` as the best available estimate.
The two values are typically within 5% of each other when the model_rates.yml is
current.

### per_turn array

Each element of `per_turn` records the token usage and cost for a single LLM turn.
This allows you to identify expensive turns (large context loads, many tool outputs)
and optimise prompt design. The `model` field in each turn can differ from the
identity block's `model` field if the runner switches models mid-run.

---

## 7. Friction Block

The friction block records signals that indicate the agent struggled.

    "friction": {
      "tool_errors":         3,                     // integer — tool call failures
      "tool_error_types": ["bash:exit_1", "write_file:permission_denied", "bash:exit_1"],
      "retries":             2,                      // integer — orchestrator-level retries
      "review_cycles":       1,                      // integer — how many review iterations occurred
      "clarification_comments": 0,                  // integer — agent posted "need clarification" comments
      "scope_violations":    0                       // integer — review:scope-violation was applied
    }

### What tool_errors measures

`tool_errors` counts the number of times a tool invocation returned an error (non-
zero exit for bash, exception for file operations, HTTP error for API calls). This
is distinct from the agent giving up (which produces an exit code 1 from the runner
itself). High tool_error counts indicate fragile test environments, missing
dependencies, or permissions issues in the CI runner.

`tool_error_types` is an array of strings in the format `<tool>:<error_class>`. The
array has the same length as `tool_errors`. Error classes are normalised:
  - bash:exit_N (N = exit code)
  - bash:timeout
  - read_file:not_found
  - write_file:permission_denied
  - api:http_N (N = HTTP status code)

### What retries measures

`retries` counts orchestrator-level retries: cases where the runner exited with code
3 (transient error) and the orchestrator re-dispatched it. Runner-internal retries
(e.g., the LLM rate-limited and the runner waited and retried) are not counted here
— those are invisible to the orchestrator.

---

## 8. Context Block

The context block records information about the input context provided to the agent.

    "context": {
      "issue_title":          "Add hello-world endpoint",    // string
      "issue_body_length":    420,                            // integer — characters
      "issue_labels":         ["type:feature", "status:in-progress", "agent:builder"],
      "issue_comment_count":  2,                             // integer — comments at run start
      "pr_diff_size":         null,                          // integer|null — chars in diff (reviewer only)
      "context_tokens_start": 3200,                          // integer — tokens in first-turn context
      "context_tokens_end":   41000,                         // integer — tokens in last-turn context
      "context_inflation_ratio": 12.8,                       // float — end/start
      "files_in_repo":        47,                            // integer — total files at run start
      "repo_size_kb":         1240                           // integer — repo size at run start
    }

### context_inflation_ratio

`context_inflation_ratio` = `context_tokens_end` / `context_tokens_start`. It
measures how much the context window grew over the course of the run. A ratio > 10
often indicates the agent is accumulating tool outputs without summarising them —
a pattern that leads to compaction events and high costs.

Healthy runs typically have a ratio between 2 and 6. Ratios above 15 should
trigger investigation: is the agent reading unnecessarily large files? Is it
accumulating bash output that could be truncated?

---

## 9. Clean Exit Block

The clean exit block records how the run ended.

    "exit": {
      "status":    "success",                        // string enum — see below
      "exit_code": 0,                                // integer — runner process exit code
      "error":     null                              // object|null — error details if status != success
    }

### status enum

    success           Run completed normally; PR opened (builder) or verdict rendered (reviewer).
    blocked           Run could not complete; status:blocked was applied to the issue.
    timeout           Runner was killed by the AGENTOS_TIMEOUT limit.
    cancelled         Run was manually cancelled (workflow cancellation in GitHub Actions).
    partial           Run made progress but requires human input (exit code 2).
    error             Unexpected error — error object is populated.

### error object

When status is `error`, the `error` object is populated:

    "error": {
      "type":    "ToolExecutionError",
      "message": "bash tool exited with code 127: command not found: pytest",
      "turn":    7,
      "tool":    "bash"
    }

For status values other than `error`, the `error` field is null.

---

## 10. Linkage Block

The linkage block connects this run record to related GitHub objects and to
previous/next runs in the same issue lifecycle.

    "linkage": {
      "pr_number":        123,          // integer|null — PR opened by this run
      "pr_url":           "https://github.com/my-org/my-repo/pull/123",
      "issue_number":     42,           // integer — always set
      "issue_url":        "https://github.com/my-org/my-repo/issues/42",
      "previous_run_id":  "my-org/my-repo:issue-42:run:20260115T120000Z",  // string|null
      "next_run_id":      null,         // string|null — populated by watcher at settlement
      "parent_issue_number": null,      // integer|null — set if this is a planner sub-issue
      "workflow_run_id":  9876543210    // integer — duplicated from identity for join convenience
    }

### previous_run_id

`previous_run_id` is the run_key of the prior run on the same issue. It is set when
the builder is re-dispatched after a changes-requested verdict. Following the chain
of `previous_run_id` links reconstructs the full history of an issue's agent
lifecycle.

### next_run_id

`next_run_id` is null when the run record is first written. The watcher populates it
at settlement time when it knows whether a follow-on run was triggered. This
back-reference is written by appending a modified settlement record that includes the
updated linkage; the original run record is never modified.

---

## 11. The Settlement Record

The settlement record is written by the watcher when the issue lifecycle ends (PR
merged or issue closed). It is a separate JSONL line with `event: settlement`.

### Settlement schema

    {
      "schema_version": 6,
      "event":          "settlement",
      "run_key":        "my-org/my-repo:issue-42:settlement:20260115T161500Z",
      "linked_run_key": "my-org/my-repo:issue-42:run:20260115T143022Z",
      "settled_at":     "2026-01-15T16:15:00Z",
      "outcome":        "merged",
      "pr_number":      123,
      "pr_merged_at":   "2026-01-15T16:14:55Z",
      "merged_by":      "human",            // string: human|auto (auto-merge)
      "review_cycles":  1,
      "total_turns":    14,
      "total_cost_usd": 0.47,
      "wall_time_seconds": 706,
      "labels_at_close": ["type:feature", "status:merged", "agent:builder", "review:lgtm"]
    }

### outcome enum

    merged            PR was merged. The full run lifecycle completed successfully.
    cancelled         Issue was closed without a merged PR.
    superseded        A newer run on the same issue was completed; this run is obsolete.
    failed            The run ended in status:blocked and the issue was manually closed.
    timed_out         The run hit the timeout limit and the issue was manually closed.

---

## 12. Model Rates

### model_rates.yml format

agentOS reads token pricing from .agentOS/model_rates.yml to compute
`modeled_cost_usd`. The file format:

    version: 1
    updated: "2026-01-01"
    rates:
      claude-opus-4-5:
        input_per_million:       15.00
        output_per_million:      75.00
        cache_read_per_million:   1.50
        cache_write_per_million:  3.75
      claude-sonnet-4-5:
        input_per_million:        3.00
        output_per_million:      15.00
        cache_read_per_million:   0.30
        cache_write_per_million:  0.75
      gpt-4o:
        input_per_million:        5.00
        output_per_million:      15.00
        cache_read_per_million:   2.50
        cache_write_per_million:  null    # not applicable for this model

All prices are in USD per million tokens. `null` means the model does not support
that token category (cache tokens are counted as regular input tokens).

### Staleness warning

Model pricing changes without notice. The `updated` field is a reminder of when the
rates were last verified — it is not enforced. When `modeled_cost_usd` diverges
significantly from `total_cost_usd` (more than 10%), update model_rates.yml and
re-run corpus_analytics.py with `--recompute-costs` to refresh historical estimates.

The agentOS CLI will warn during `agentOS verify` if model_rates.yml has not been
updated in more than 90 days:

    [WARN] model_rates.yml was last updated 2025-10-01 (107 days ago). Costs may be inaccurate.

### How modeled_cost_usd is computed

For each turn in the `per_turn` array:

    turn_cost = (input_tokens / 1_000_000 * input_per_million)
              + (output_tokens / 1_000_000 * output_per_million)
              + (cache_read_tokens / 1_000_000 * cache_read_per_million)  if applicable
              + (cache_write_tokens / 1_000_000 * cache_write_per_million) if applicable

`modeled_cost_usd` = sum of `turn_cost` across all turns.

---

## 13. Using the Corpus

### corpus_analytics.py

The agentOS CLI ships a corpus analytics script at:

    .agentOS/corpus_analytics.py

Run it against one or more monthly JSONL files:

    python3 .agentOS/corpus_analytics.py .agentOS/logs/runs-*.jsonl

Produces a summary report in the terminal:

    Corpus summary
    ==============
    Files:           12
    Run records:     247
    Settlement records: 241
    Unsettled runs:  6

    Outcomes
    --------
    merged:          198  (82.2%)
    cancelled:        19  (7.9%)
    failed:           18  (7.5%)
    timed_out:         6  (2.5%)

    Cost (settled runs)
    -------------------
    Total cost:      $184.27
    Avg per run:     $0.76
    Median per run:  $0.52
    p95 per run:     $2.14

    Performance
    -----------
    Avg turns:       12.4
    Avg tool_calls:  31.7
    Avg duration:    9m 42s
    Avg queue:       1m 58s

    Friction
    --------
    Avg tool_errors: 1.2
    Runs with retries: 14 (5.7%)
    Avg review_cycles: 1.3

### The dashboard

For a live dashboard, corpus_analytics.py can emit a JSON summary file:

    python3 .agentOS/corpus_analytics.py .agentOS/logs/runs-*.jsonl \
      --output-json .agentOS/dashboard-data.json

This JSON file can be consumed by Grafana (JSON API data source), Observable, or
any BI tool. The agentOS GitHub repo includes a sample Grafana dashboard definition
at .agentOS/grafana-dashboard.json.

### Filtering and slicing

corpus_analytics.py supports filtering:

    # Only builder runs
    --role builder

    # Only runs from the last 30 days
    --since 2026-01-01

    # Only runs that hit status:blocked
    --outcome failed

    # Export matching records as a new JSONL file
    --export filtered.jsonl

    # Recompute modeled_cost_usd using current model_rates.yml
    --recompute-costs
