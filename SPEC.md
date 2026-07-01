# GitHub AgentOS Specification

**Version:** 1.0-draft
**Status:** Draft
**Org:** https://github.com/open-agentos

---

## 1. Purpose and Scope

This document is the normative specification for the GitHub AgentOS system. It defines:

1. The label model and state machine that drives agent routing
2. The agent role model and permission contract
3. The runtime interface that any agent runner must satisfy
4. The GitHub Actions workflow contract (triggers, outputs, receipts)
5. The Projects v2 board field contract
6. The JSONL run-record metrics schema
7. The plugin interface for extending the core system

This spec does NOT define:
- The content of agent system prompts (that is the operator's responsibility)
- Which LLM provider or model to use (configurable per-deployment)
- Project-specific workflows (those belong in plugins)

Implementations that conform to this spec are interoperable: any AgentOS-compliant agent
runner can be dropped into any AgentOS-provisioned repository.

---

## 2. Terminology

MUST / MUST NOT / SHOULD / SHOULD NOT / MAY follow RFC 2119.

  Agentos repo     The agentOS repository. Contains the agentOS.yaml file,
                bootstrap CLI, templates, and this document.

  Target repo   The GitHub repository being provisioned. The user's project.

  Agent         An automated process that reads GitHub context, performs work, and
                updates GitHub state (labels, PRs, issue comments, board fields).

  Role          A named agent identity with a defined permission scope and a set of
                status labels that trigger it. Core roles: builder, reviewer, watcher, board.

  Runner        The CLI command that executes an agent for a given issue. The spec defines
                the runtime interface (env vars, exit codes). The runner is user-supplied.

  Operator      The person who imports and deploys the spec to a target repo.

  Plugin        An opt-in extension to the core spec. Plugins add labels, board fields,
                workflows, and agent config without modifying the core spec file.

---

## 3. Label Model

### 3.1 Axes

Labels are organised into axes. Each axis has a string prefix (e.g. "status:") and a set
of values. The full label name is "{axis_prefix}{value}" (e.g. "status:todo").

The bootstrap provisions all labels defined in agentOS.yaml idempotently (upsert by name).

Core axes:

  status    Lifecycle state of an issue. The PRIMARY ROUTING TRIGGER. Changing a
            status label is the mechanism by which agents and humans hand off work.

  agent     Current ownership. Set by agents to declare they hold an issue.
            Read by humans and dashboards. Does NOT trigger workflows.

  type      Issue classification. Used for board filtering and routing heuristics.

  review    Reviewer verdict sub-flags. Provides fine-grained signal alongside
            status:changes-requested.

  source    Issue origin provenance. Set at creation time. Enables filtering by
            whether an issue was created by a human or an agent.

  follow-on Async handoff signals. Labels in this axis declare that a follow-on
            action is needed. Their routing behaviour is configurable (see 3.4).

### 3.2 Status State Machine

The status axis defines a finite state machine. Transitions are driven by label events
in GitHub Actions. The following diagram shows the core transition graph:

```
                      [human creates issue]
                              |
              .---------------+---------------.
              |                               |
              v                               v
       status:plan                      (no planning)
       (optional; triggers planner)          |
              |                              |
              v                              |
      status:plan-review                     |
      (awaiting /approve-plan)               |
              |                              |
    admin /approve-plan                      |
    (permission verified live)               |
              |                              |
              +------------------------------+
              |
              v
         status:todo            --> builder agent (gated by approval check)
              |
    .---------+---------.
    |                   |
    v                   v
status:in-review   status:blocked    (human resolves)
    |
.---------+---------.
|                   |
v                   v
status:approved  status:changes-requested --> builder agent (retry)
    |
    v
status:done              (set automatically on PR close / issue close)
```

Conforming implementations MUST support at minimum: todo, in-review, changes-requested,
approved, blocked, done. The planning states `status:plan` and `status:plan-review` are
required when the planner is enabled (default). `governance.planning: off` is the only
mode in which these states are inactive.

### 3.3 Routing Table

This table defines which agent role is triggered by each status label:

  status:plan              -> planner (dispatched on label event; concurrency-guarded)
  status:plan-review       -> no agent; awaiting /approve-plan from authorised approver
  status:todo              -> builder (only after approval gate passes; see §3.6)
  status:in-review         -> reviewer
  status:changes-requested -> builder
  status:approved          -> docs (if enabled; else no-op)
  status:blocked           -> no agent; human intervention required
  status:done              -> no agent; terminal state
  status:in-progress       -> no agent; informational only

### 3.4 Follow-on Label Routing

Labels in the follow-on axis MAY be configured to trigger a specific agent role. The
routing behaviour is defined in agentOS.yaml under each follow-on label's `routes_to`
field. If `routes_to` is null the label is informational only.

Example: `follow-on:docs-needed` with `routes_to: docs` causes the orchestrator to
dispatch the docs agent when this label is applied, regardless of the current status.

### 3.5 Label Idempotency

The bootstrap MUST:
- Create a label if it does not exist (POST /repos/{owner}/{repo}/labels)
- Update a label's colour if it exists but the colour differs (PATCH)
- Skip a label if it exists and the colour matches (no-op)
- NEVER delete labels not in the spec (labels may be user-created)

### 3.6 Planning Stage and Dispatch-time Approval

#### 3.6.1 The two planning states

`status:plan` and `status:plan-review` are first-class, visible status states.

  status:plan
    Entry point for the planning stage. Applying this label dispatches the planner.
    The planner fires ONLY on this label — it does not fire on `status:todo`.

  status:plan-review
    Set by the planner when it has written a plan into the issue body. No agent is
    dispatched. The issue waits for a human approval command.

Conforming implementations MUST support both states when the planner is enabled.

#### 3.6.2 The marker contract

The plan lives in the issue body between two HTML comment markers:

    <!-- AGENTOS:PLAN:BEGIN -->
    (plan content, CE-style template)
    <!-- AGENTOS:PLAN:END -->

Rules:
  - The planner MUST replace the content between the markers on each run (never append).
  - Content above the BEGIN marker is the human's original intent and MUST be preserved.
  - The builder reads the plan block as its authoritative implementation contract.
  - When no markers are present and planning is `optional` or `off`, the builder uses
    the full issue body.

#### 3.6.3 Dispatch-time approval semantics

Approval is NOT a label. It is a live permission check performed by the orchestrator
at the moment of builder dispatch.

A build MUST NOT be dispatched unless ALL of the following conditions hold:

  1. The issue body contains a plan block between the markers.
     (Requirement waived when `governance.planning` is `optional` or `off`.)

  2. An `/approve-plan` comment exists from a user whose GitHub collaborator
     permission level (checked live via the GitHub API at dispatch time) is in
     `governance.approvers`. The check MUST be performed at dispatch, not cached.

  3. That approval comment's `created_at` MUST be later than the timestamp of the
     most recent plan receipt comment on the issue. A stale approval that predates
     the current plan revision DOES NOT authorise a build.

Applying `status:todo` without a valid approval comment MUST result in no build
being dispatched. The orchestrator silently skips the builder dispatch.

#### 3.6.4 Slash commands

The orchestrator listens for `issue_comment` events with these commands:

  /approve-plan
    Triggers the approval check. If the commenter has an approver-level permission
    and all build conditions hold, the orchestrator transitions the issue to
    `status:todo` and dispatches the builder. If the commenter lacks permission,
    the orchestrator posts a polite refusal and takes no further action.

  /request-changes <notes>
    Sent by an approver to request plan revisions. The orchestrator transitions
    the issue back to `status:plan`, which re-triggers the planner. The planner
    incorporates the notes into the revised plan (they appear as comments on the
    issue). The orchestrator MUST verify the commenter has approver-level permission
    before honouring this command.

#### 3.6.5 governance config block

The `governance:` block in agentOS.yaml configures the planning and approval gate:

```yaml
governance:
  planning: required          # required | optional | off
  approvers: [admin]          # GitHub collaborator permission levels
  approve_command: "/approve-plan"
  changes_command: "/request-changes"
  plan_begin_marker: "<!-- AGENTOS:PLAN:BEGIN -->"
  plan_end_marker:   "<!-- AGENTOS:PLAN:END -->"
```

  planning: required (default)
    The planner MUST run and an admin MUST approve before the builder runs.

  planning: optional
    Issues MAY go straight to `status:todo`. A plan block is not required.
    An admin approval is still required.

  planning: off
    Legacy mode. The builder fires on `status:todo` unconditionally. No approval
    check is performed. `status:plan` and `status:plan-review` have no effect.

#### 3.6.6 Idempotency and concurrency

The orchestrator MUST use a `concurrency:` group keyed on the issue number for the
planner job. This prevents two planner runs from racing on the same issue. A second
`status:plan` event cancels the in-flight planner run. This replaces the need for a
"planner is busy" state label.

---

## 4. Agent Role Model

### 4.1 Core Roles

Four roles are defined in core. All four MUST be provisioned for a conforming deployment.

  builder
    Implements features and fixes. Opens pull requests. The only role with write access
    to repository contents. Also handles retry after changes-requested.
    Permissions: contents:write, issues:write, pull_requests:write, metadata:read,
                 workflows:write

  reviewer
    Reviews pull requests. Approves or requests changes. MUST NOT have write access to
    repository contents (this is an intentional security constraint — a reviewer that can
    push code can circumvent its own review).
    Permissions: issues:write, pull_requests:write, metadata:read, checks:write

  watcher
    Minimal-footprint role. Used for: settlement (updating board after PR close), issue
    creation (creating follow-on issues), and scheduled monitoring tasks. Plugins that add
    scheduled behaviours (e.g. a daily question generator) run as watcher.
    Permissions: issues:write, metadata:read

  board
    Projects v2 mutations only. Used exclusively to update board fields. No repository
    write access. MUST use organization_projects:write (not repository_projects).
    Permissions: organization_projects:write, metadata:read

### 4.2 Optional Roles

Two additional roles are defined in core but disabled by default:

  docs
    Updates documentation and changelog after approved PRs. Reuses the builder GitHub App
    (same credentials, same permissions). Enabled by uncommenting in agentOS.yaml.
    Triggers on: status:approved, follow-on:docs-needed (if configured)

  planner
    Turns a thin human-written issue into a concrete, file-level plan written into the
    issue body between AGENTOS:PLAN:BEGIN/END markers. Core role, enabled by default.
    Triggers on: status:plan
    Permissions needed: issues:write only (currently reuses builder App as a pragmatic
    shortcut; splitting to a dedicated App with issues:write only is a recommended TODO).
    See §3.6 for the full planning stage and approval gate specification.

### 4.3 GitHub App Identity

Each role that has `create_app: true` in agentOS.yaml gets its own GitHub App. Each
App is installed on the target repo and mints short-lived installation tokens at runtime.

Roles with `reuse_app: {other_role}` share the GitHub App of the named role. This is
acceptable for roles with identical permission requirements (docs and planner both need
the same capabilities as builder).

Token minting: the bootstrap provides github_token.py (scripts/github_token.py in the
target repo after `agentOS apply`). At runtime: JWT signed with the app's private key ->
POST /app/installations/{id}/access_tokens -> short-lived token (1 hour TTL).

Credential storage:
  Local development:  .env file (GITHUB_APP_ID_{ROLE}, GITHUB_APP_PRIVATE_KEY_{ROLE})
  GitHub Actions:     repository secrets (same names, set by `agentOS setup`)

### 4.4 Branch Naming Convention

Agent branches MUST follow the pattern:

  agent/{role}/{issue_number}-{slug}

Examples:
  agent/builder/42-add-user-auth
  agent/reviewer/42-add-user-auth

The run-receipt workflow parses the issue number from this pattern. Branches that do not
match this pattern will not receive run receipts.

---

## 5. Runtime Interface

The spec is runtime-agnostic. Any CLI tool that satisfies this interface can be used as
the agent runner.

### 5.1 Environment Variables

The orchestrator workflow injects these env vars before invoking the runner command:

  REQUIRED:
    AGENT_ROLE           The role being executed (builder / reviewer / watcher / etc.)
    ISSUE_NUMBER         The GitHub issue number (integer as string)
    GITHUB_TOKEN         Short-lived GitHub App installation token for this role
    GITHUB_REPOSITORY    owner/repo (standard GHA variable)
    GITHUB_RUN_ID        The Actions run ID (standard GHA variable)

  OPTIONAL (set if configured):
    LLM_PROVIDER         Provider identifier (e.g. "anthropic", "openai", "cloudflare")
    LLM_MODEL            Model identifier (e.g. "claude-sonnet-4-6")
    LLM_API_KEY          API key for the provider
    AGENT_MAX_TURNS      Maximum turns budget for this run (integer)
    OPS_REPO             owner/repo of the ops/metrics repository (if separate)
    OPS_REPO_TOKEN       PAT with read/write access to the ops repo

### 5.2 Exit Codes

  0    Clean exit. Agent completed its task successfully.
  1    Crashed / error. The orchestrator will post a failure comment to the issue.
  2    Max turns reached. Treated as a soft failure; agent should set status:blocked
       before exiting if it cannot make progress.

### 5.3 Runner Command Configuration

The runner command is specified in config.yaml:

```yaml
agent:
  runner: "hermes run"       # or: claude, codex, my-custom-runner
```

The orchestrator workflow calls: `{runner} $AGENT_ROLE` or equivalent, after injecting
all env vars into the shell environment.

Implementations MAY use a more complex invocation (e.g. passing flags). The runner command
is a shell string passed to bash -c.

### 5.4 Agent Scaffold

After `agentOS apply`, the target repo contains:

  AGENTS.md                     Operating manual for all agents. Defines roles,
                                 execution protocol, state machine, guardrail rules.
  agents/
    _shared/
      context-management.md     How to manage context window across long runs
      escalation.md             When and how to escalate to human
      loop.md                   Loop guardrail rules (no infinite loops)
      telemetry.md              How to emit run records
    {role}/
      AGENT.md                  Role-specific instructions (fill in by operator)

AGENT.md files MUST contain at minimum:
  # Role: {role name}
  ## Purpose
  ## Constraints
  ## Output Format
  ## Handoff Protocol

---

## 6. Workflow Contract

### 6.1 agent-orchestrator.yml

  Triggers:
    issues:   [opened, labeled, closed]
    pull_request: [labeled]

  On issue labeled with a status:* label:
    1. Read the label name
    2. Look up the routing table (label -> role)
    3. If a role is found and is enabled: dispatch the runner
    4. Set agent:{role} label on the issue
    5. Call run-receipt.yml as a reusable workflow when complete

  On issue closed (without status:done):
    1. Set status:done label

  On type:feature issue opened:
    1. Add issue to the Projects v2 board (if board is enabled)

  On follow-on:* label applied:
    1. Look up configured routes_to for that follow-on label
    2. If routes_to is set: dispatch the named role

### 6.2 agent-settlement.yml

  Triggers:
    pull_request: [closed]

  On PR closed (merged or unmerged):
    1. Determine linked issue number (from PR body or branch name)
    2. Mint a board token (watcher role)
    3. Run projector.py: update the Outcome field on the board item
    4. Set status:done on the linked issue (if merged)

### 6.3 detect-run-failure.yml

  Triggers:
    workflow_run: workflows: ["Agent Orchestrator"], types: [completed]
    condition: conclusion == 'failure'

  On failure:
    1. Fetch failed job details via GitHub API
    2. Parse issue number from failed job name or branch name
    3. Post a structured escalation comment to the issue
    4. If issue cannot be determined: fall back to issue #1

### 6.4 run-receipt.yml (reusable)

  Triggers:
    workflow_call: inputs: job_status (string, required)

  Condition: branch name matches agent/{role}/{number}-{slug}

  Actions:
    1. Parse issue number and role from branch name
    2. Compute duration from workflow start time
    3. Post a machine-parseable receipt comment to the issue

  Receipt comment format:
  ```
  <!-- agentOS:run-receipt -->
  **Run Receipt** | Role: {role} | Status: {status} | Duration: {duration}s
  Run ID: {run_id} | Branch: {branch}
  <!-- /agentOS:run-receipt -->
  ```

---

## 7. Projects v2 Board Contract

### 7.1 Field Definitions

The board has 10 fields divided into three flow categories:

  METADATA (set by operator or orchestrator before/during run):
    Role          single_select   Maps agent:* ownership label to a board value
    Status        single_select   Mirrors status:* label (denormalised for board UI)
    Max turns     number          Per-issue turn budget. Default: 40.

  PARAMETER (configures agent behaviour for this issue):
    Model         single_select   Which LLM to use. Default options (see 7.2).

  TELEMETRY (written by runner after each run, read by dashboards):
    Outcome       single_select   reduce: latest_settlement
    Clean exit    single_select   reduce: last_run
    Cost to date  number          reduce: sum_runs
    Turns         number          reduce: sum_runs
    Attempts      number          reduce: count_runs

### 7.2 Default Model Options

The Model field ships with these five options (values are display names; operators map
them to actual model IDs in config.yaml):

  claude-haiku          Anthropic Claude Haiku — fast, cheap, best for routine tasks
  claude-sonnet         Anthropic Claude Sonnet — balanced capability and cost
  gpt-4o-mini           OpenAI GPT-4o Mini — fast general-purpose option
  gemini-flash          Google Gemini Flash — fast multimodal option
  kimi-k2               Moonshot Kimi K2 — strong coding performance

Operators extend or replace this list in their agentOS.yaml or via a plugin.

### 7.3 Reduce Semantics

  latest_settlement     The value from the most recent settlement event wins.
                        Used for Outcome: a reverted PR should update the Outcome,
                        not keep the original "Merged" value.

  last_run              The value from the most recent run event wins.
                        Used for Clean exit: reflects the last run's exit status.

  sum_runs              Values from all run events for this issue are summed.
                        Used for Cost to date, Turns, Attempts.

  count_runs            Count of run events. Used for Attempts.

### 7.4 Schema Fingerprinting

The bootstrap computes a SHA-256 fingerprint of the field definition block in agentOS.yaml
and stores it in field-bindings.json alongside the live GraphQL node IDs:

```json
{
  "schema_fingerprint": "sha256:...",
  "board_id": "PVT_...",
  "fields": { ... }
}
```

On re-run, if the fingerprint matches, the board provisioning step is skipped. If the
fingerprint differs, the fields are re-synced. New options are added; existing options
are never deleted (GitHub Projects v2 does not support option deletion via API).

---

## 8. Metrics Schema

### 8.1 Run Record (v6)

Every agent invocation MUST produce a run record appended to the JSONL corpus
(ops-metrics/{YYYY-MM}.jsonl). Fields:

  TOP LEVEL:
    schema_version    int      Always 6
    event             str      "run" | "settlement"
    run_key           str      "{repo}|{role}|{kind}|{issue}|{run_id}|{attempt}"

  IDENTITY block:
    repo              str      "owner/repo"
    role              str      Agent role name
    kind              str      "issue" | "pr"
    number            int      Issue or PR number
    agent_identity    str      GitHub App slug
    run_id            str      GitHub Actions run ID
    attempt           int      1-indexed attempt count
    github_actions_run_url str
    model_provider    str
    model_name        str

  LIFECYCLE block:
    started_at        str      ISO 8601
    ended_at          str      ISO 8601
    duration_seconds  float

  EXECUTION block:
    turns             int
    tool_calls        int
    max_turns_hit     bool
    compaction        object   Context compaction events (see schema JSON)

  COST block:
    input_tokens      int
    output_tokens     int
    total_tokens      int
    input_cost_usd    float
    output_cost_usd   float
    total_cost_usd    float
    modeled_cost_usd  float    Cost if computed from model_rates.yml
    per_turn          array    [{input: int, output: int}]

  FRICTION block:
    tool_errors       int
    retries           int
    repeats           int
    max_turns_proximity float  Turns / max_turns ratio
    tool_error_breakdown array [{tool: str, count: int}]

  CONTEXT block:
    diff_lines_added  int
    diff_lines_removed int
    files_changed_count int
    issue_labels      array[str]
    model_version     str
    context_inflation_ratio float

  CLEAN_EXIT block:
    status            str      "clean" | "crashed" | "max_turns" | "infra_failure"
    detail            str
    error             object | null   {error_type, tool, code}

  LINKAGE block:
    pr_number         int | null
    issue_number      int
    previous_run_id   str | null

  outcome             str      "provisional" | "merged" | "closed_unmerged" |
                               "ci_failed" | "reverted" | "abandoned"

### 8.2 Settlement Record (v6)

  schema_version    int      6
  event             str      "settlement"
  run_key           str      "settlement|{repo}|{pr_number}"
  settled_at        str      ISO 8601
  outcome           str      See outcome values above
  ci_result         str | null
  reviewer_verdict  str | null
  reverted_at       str | null
  reverted_by       str | null
  pr_number         int

### 8.3 Cost Accounting

Model rates are stored in scripts/model_rates.yml:

```yaml
# NOTICE: Rates are approximate and may be stale. Verify with your provider.
# Last updated: 2026-06-26
anthropic:
  claude-haiku-4-5-20251001:
    input_rate_usd_per_m: 0.80
    output_rate_usd_per_m: 4.00
    context_window: 250000
```

The `modeled_cost_usd` field is computed using this table. If the provider+model is not
in the table, `modeled_cost_usd` is null and `total_cost_usd` relies on API-reported costs.

---

## 9. Plugin Interface

### 9.1 Plugin Manifest

A plugin is a directory containing a plugin.yaml manifest. It may also contain:
  labels.yml       Additional labels to provision
  workflows/       Additional GHA workflow files to copy to .github/workflows/
  agents/          Additional or override AGENT.md content per role
  scripts/         Additional scripts to copy to the target repo's scripts/

plugin.yaml structure:
```yaml
pluginVersion: "1.0"
name: "my-plugin"
description: "What this plugin does"
specVersionRequired: ">=1.0"   # semver range

labels:
  - axis: phase
    values:
      - name: "1"
        color: "c2e0c6"

board_fields:
  - name: Sprint
    type: text
    flow: metadata

follow_on_routes:
  docs-needed:
    routes_to: docs

workflows:
  - source: workflows/my-workflow.yml
    target: .github/workflows/my-workflow.yml
    enabled_by_default: true
```

### 9.2 Plugin Loading

Plugins are listed in agentOS.yaml:
```yaml
plugins:
  - name: three-questions
    source: github:open-agentos/agentos//plugins/three-questions@v1.1.0
```

Sources supported in v1.0:
  github:{owner}/{repo}//{path}@{ref}   Remote GitHub path (downloaded at apply time)
  local:{path}                           Local filesystem path (relative to agentOS.yaml)

The bootstrap applies plugins in order after core provisioning. Plugins MUST NOT modify
core-provisioned resources (they may only add). If a plugin attempts to modify a core
label's colour, the bootstrap MUST warn and skip that change.

### 9.3 Reference Plugin: three-questions

The three-questions plugin ships in this repo at plugins/three-questions/ and demonstrates
the full plugin interface. It adds:
  - phase:* labels (project milestone tracking)
  - follow-on:dreaming-needed label
  - follow-on:docs-needed label (routed to docs agent)
  - Watcher scheduled workflow (daily intelligence brief generation)
  - Watcher AGENT.md template with source configuration

---

## 10. Bootstrap CLI Contract

### 10.1 Commands

  agentOS init [--from {source}]
    Generates agentOS.yaml in the current directory. If --from is provided, downloads
    and uses that spec as the starting point. Otherwise generates a blank spec with
    prompts for operator choices.

  agentOS setup --repo {owner/repo}
    Interactive GitHub App registration wizard. For each role with create_app: true,
    opens the GitHub App manifest flow in a browser, receives the OAuth callback on
    localhost:4000, and writes credentials to .env and to GHA repo secrets.
    Requires: gh CLI authenticated, GITHUB_TOKEN in environment.

  agentOS apply --repo {owner/repo} [--labels-only] [--board-only] [--workflows-only] [--force]
    Provisions the target repo from agentOS.yaml. Runs all steps unless a --only flag
    limits scope. Idempotent. Tracks progress in .agentOS-state.json. Safe to re-run.

  agentOS verify --repo {owner/repo}
    Checks that the target repo matches agentOS.yaml. Reports pass/fail per component.
    Exit 0 if all pass. Exit 1 if any fail.

### 10.2 State File

  .agentOS-state.json tracks bootstrap progress:
  ```json
  {
    "spec_fingerprint": "sha256:...",
    "repo": "owner/repo",
    "steps": {
      "labels": {"status": "complete", "at": "2026-06-26T10:00:00Z"},
      "board": {"status": "complete", "at": "2026-06-26T10:01:00Z"},
      "workflows": {"status": "failed", "at": "2026-06-26T10:02:00Z", "error": "..."},
      "apps": {"status": "pending"}
    }
  }
  ```
  On re-run, steps with status "complete" whose input fingerprint matches are skipped.
  Steps with status "failed" or "pending" are retried.

---

## 11. Conformance

A deployment is AgentOS-conformant if:
  - All required labels (status:*, agent:*, type:*, review:*, source:*) are present
  - The Projects v2 board has all 10 required fields with correct types
  - The four core GitHub Apps are installed with the specified permission scopes
  - The orchestrator workflow fires on issue label events AND issue_comment events
    and routes per the routing table (§3.3)
  - The orchestrator enforces the dispatch-time approval gate (§3.6.3) before
    dispatching the builder on status:todo
  - The settlement workflow fires on PR close and updates the Outcome field
  - Each agent invocation produces a valid v6 run record
  - The planner role emits run records with identity.role = "planner"

Plugins may extend a conformant deployment without breaking conformance.

---

## Appendix A: Colour Reference

These are the canonical label colours used by the core spec. Operators MAY change colours;
the routing logic is based on label names, not colours.

  status:plan              c5def5   Light blue (planner entry)
  status:plan-review       e4e669   Yellow (awaiting approval)
  status:todo              ededed   Light gray
  status:in-progress       0075ca   Blue
  status:in-review         fbca04   Yellow
  status:changes-requested d93f0b   Red-orange
  status:approved          0e8a16   Green
  status:blocked           b60205   Dark red
  status:planning          bfd4f2   Light blue (legacy; kept for back-compat)
  status:done              0e8a16   Green

  agent:builder            1d76db   Blue
  agent:reviewer           cc317c   Pink
  agent:docs               5319e7   Purple
  agent:watcher            0075ca   Blue
  agent:planner            f9d0c4   Salmon

  type:feature             84b6eb   Light blue
  type:bug                 ee0701   Red
  type:chore               fef2c0   Cream
  type:question            d876e3   Lavender

  review:scope-violation   b60205   Dark red

  source:agent-created     0e8a16   Green
  source:human-created     bfd4f2   Light blue

---

## Appendix B: Runtime Interface Env Var Reference

  AGENT_ROLE              string    Required. Role name.
  ISSUE_NUMBER            string    Required. Issue number (integer as string).
  GITHUB_TOKEN            string    Required. App installation token.
  GITHUB_REPOSITORY       string    Required. "owner/repo" (set by GHA).
  GITHUB_RUN_ID           string    Required. Actions run ID (set by GHA).
  LLM_PROVIDER            string    Optional. Provider slug.
  LLM_MODEL               string    Optional. Model identifier.
  LLM_API_KEY             string    Optional. API key.
  AGENT_MAX_TURNS         string    Optional. Integer string.
  OPS_REPO                string    Optional. "owner/ops-repo".
  OPS_REPO_TOKEN          string    Optional. PAT for ops repo.

---

## Appendix C: Plan Body Template (CE-style)

The planner MUST use this template between the AGENTOS:PLAN:BEGIN/END markers.
The builder reads the content between these markers as its authoritative contract.

```markdown
<!-- AGENTOS:PLAN:BEGIN -->
### Plan

**Problem / intent**
<one-paragraph restatement of what the issue is asking for>

**Context & constraints**
<relevant existing behaviour, files, invariants, hard constraints>

**Approach**
<the chosen design, and briefly why over alternatives>

**Task breakdown**
- [ ] <file-level, ordered, independently checkable step>
- [ ] <...>

**Acceptance criteria**
- <observable condition 1>

**Test plan**
<how each acceptance criterion is verified — commands, smoke steps>

**Risks & open questions**
- <risk or decision that needs a human>

**Out of scope**
- <explicitly excluded>
<!-- AGENTOS:PLAN:END -->
```

---

## Appendix D: Approval Gate Design Notes

### Why live permission check, not a stored label

An earlier design stored approval in a `review:plan-approved` label and defended it
with a guard workflow that reverted the label if applied by a non-approver. That
approach requires: a dedicated guard workflow, a GITHUB_TOKEN loop-prevention caveat
(to avoid the guard triggering itself), and a label that can still be spoofed by a
repo admin with direct label access.

The dispatch-time check (§3.6.3) is simpler and stronger:
- No label to defend, so no guard workflow needed.
- No loop-prevention caveat.
- The permission check runs against live GitHub data at the moment of dispatch.
- Manually labelling an issue to bypass the gate simply results in no build — the
  orchestrator's check fails silently and the builder is not dispatched.

This appendix documents the reasoning so it is not lost if a future requirement
(e.g. an approval signal consumed by a system that cannot re-derive it live) ever
justifies a stored approval token. In that case, see the label+guard pattern
described in the plan brief's Appendix E.
