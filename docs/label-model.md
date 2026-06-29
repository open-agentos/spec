# Label Model Reference

This document is the authoritative reference for every label axis in the agentOS
label model. Read it alongside docs/agent-roles.md (which explains which agent can
write which label) and docs/plugins.md (which explains how to add axes without
modifying the core).

---

## 1. Overview — Why Labels, Not Webhooks or Databases

### The core claim

agentOS encodes all workflow state directly in GitHub issue labels. This is a
deliberate design choice, not an omission. The alternatives are:

- External database (DynamoDB, Postgres, etc.)
- Webhook router with its own state machine
- GitHub Actions environment variables passed between jobs

Each of those alternatives introduces an out-of-band system that is invisible to
anyone browsing the repository, requires its own auth, fails silently when it
drifts from reality, and becomes a bottleneck for debugging.

Labels are:

- Visible — anyone on GitHub can see the full state of an issue at a glance.
- Auditable — the issue timeline records every label change with timestamp and actor.
- Durable — labels survive repository transfers, forks, and API outages (they are
  part of the GitHub data model, not an agentOS-specific side table).
- Filterable — GitHub's native search (label:status:in-review label:type:feature)
  works without any agentOS infrastructure running.
- Triggerable — GitHub Actions `labeled` and `unlabeled` events fire reliably and
  are the cheapest possible trigger (no polling).

### The axis model

Labels are grouped into axes. Each axis is a namespace (prefix) that answers a
distinct question about the issue:

    status:*      What is happening right now?
    agent:*       Which agent role currently owns this issue?
    type:*        What kind of work is this?
    review:*      What verdict did the reviewer reach?
    source:*      How was this issue created?
    follow-on:*   What async work is queued after the current run?

Within an axis, exactly one label should be active at any time (single-select
semantics). The orchestrator enforces this: before applying a new status:* label it
removes any existing status:* labels. The same applies to agent:* and review:*.

### Colour conventions

Colours within an axis are stable across versions. Downstream tooling (dashboards,
scripts, other labels) should rely on colour only as a human hint, never as
programmatic identity. Use the label name as the stable identifier.

---

## 2. The Status Axis

The status axis drives the agent workflow. Transitions between status labels are the
primary mechanism by which agents hand off work to each other.

### Full status label table

    Label                   Hex Colour   Description
    ----------------------  -----------  -----------------------------------------------
    status:plan             c5def5       Expand this issue into a plan (planner entry).
    status:plan-review      e4e669       Plan written; awaiting admin /approve-plan.
    status:todo             ededed       Ready to be picked up by the orchestrator.
    status:in-progress      0075ca       Builder agent is actively working on this issue.
    status:in-review        0052cc       PR is open; reviewer agent is evaluating it.
    status:approved         0e8a16       Reviewer approved; ready for merge or docs/planner.
    status:changes-requested  b60205     Reviewer requested changes; builder must iterate.
    status:blocked          d93f0b       Human attention required; no agent will touch it.
    status:merged           6f42c1       PR was merged; watcher runs settlement.
    status:closed           cfd3d7       Issue closed without merge (cancelled, duplicate).

### State machine (with planning stage)

```
[human applies status:plan]
         |
         v
   planner runs        (concurrency group: issue number — prevents parallel runs)
         |
         v
  status:plan-review   (plan written between AGENTOS:PLAN:BEGIN/END markers)
         |
  admin comments /approve-plan    (permission verified live at dispatch time)
         |
         v
    status:todo  ──────────────────────────────────────────────────────────┐
         |                                                                  |
         v                                          manual entry (trust-me admin):
   builder runs   ◄──────────────────────────────  author plan in markers,
         |                                          apply status:todo, then
         |                                          /approve-plan
         ├──────────────────────┐
         v                      v
  status:in-review        status:blocked    (human resolves)
         |
    .----+----.
    |         |
    v         v
status:     status:changes-requested ──► builder (retry)
approved
    |
    v
 status:done

Manual review-me-first path:
  author plan in markers → open at status:plan-review → admin /approve-plan → status:todo → builder
```

Notes:
- `status:plan` is the planner entry point. Applying it fires the planner.
- `status:plan-review` means "plan written; no agent dispatch — awaiting human."
- The builder fires on `status:todo` only when the orchestrator confirms a valid
  `/approve-plan` from an authorised approver that postdates the latest plan.
  Applying `status:todo` without that approval is a no-op (no build dispatched).
- `governance.planning: optional` lets issues skip planning and go straight to
  `status:todo` + `/approve-plan`.
- `governance.planning: off` restores legacy behaviour (builder fires on `status:todo`
  unconditionally; no approval check).

### routes_to configuration

Each status label has an optional `routes_to` field in agentOS.yaml that tells the
orchestrator which agent role to dispatch when that label is applied:

    labels:
      status:
        todo:
          routes_to: builder        # gated: builder only fires after approval check (see §2 notes)
        in-review:
          routes_to: reviewer
        approved:
          routes_to: null          # no dispatch; merge happens via branch protection
        changes-requested:
          routes_to: builder       # builder iterates
        blocked:
          routes_to: null          # human-only; agents skip
        merged:
          routes_to: watcher       # settlement
        closed:
          routes_to: watcher       # settlement (no-op outcome)
        plan:
          routes_to: planner       # planner fires on this label
        plan-review:
          routes_to: null          # no agent; awaiting human /approve-plan

`routes_to: null` means the orchestrator acknowledges the transition but does not
dispatch any agent. A human or a protected-branch merge event moves the issue onward.

### What happens on each transition

status:plan
  The issue has been opted into the planning stage. Applying this label fires the
  orchestrator, which dispatches the planner role. The planner reads the issue body,
  generates a concrete file-level plan using the CE-style template, rewrites the body
  so the original intent is preserved at the top and the plan sits between
  `<!-- AGENTOS:PLAN:BEGIN -->` and `<!-- AGENTOS:PLAN:END -->` markers, then
  transitions to `status:plan-review`. A concurrency group keyed on the issue number
  prevents two planner runs from overlapping on the same issue.

status:plan-review
  The planner has written a plan into the issue body and the issue is awaiting human
  review. No agent is dispatched on this status. An authorised approver (default:
  admin) reviews the plan and comments `/approve-plan` to proceed, or
  `/request-changes <notes>` to send the issue back to `status:plan` for a revised
  plan. The orchestrator also watches for `issue_comment` events so it can act on
  these commands in real time.

status:todo
  The issue is in the backlog and ready for automation. Applying this label (while a
  type:* label is already present) fires the orchestrator. The orchestrator dispatches
  the builder role, removes status:todo, and applies status:in-progress + agent:builder.

status:in-progress
  The builder is running. No other agent touches the issue. If the builder exits with
  a non-zero code before opening a PR, the issue is moved to status:blocked and a
  comment is written explaining the failure.

status:in-review
  The builder opened a PR and applied this label. The orchestrator dispatches the
  reviewer. The reviewer reads the diff and either approves (-> status:approved) or
  requests changes (-> status:changes-requested). The reviewer also applies one or
  more review:* sub-flags before changing the status.

status:approved
  The reviewer approved the PR. No agent is dispatched. If the `docs` optional role
  is enabled, the docs agent is dispatched to update documentation. If branch
  protection auto-merge is enabled, the PR merges automatically. On merge, GitHub
  fires the pull_request.closed event; the watcher transitions to status:merged.

status:changes-requested
  The reviewer requested changes. The orchestrator re-dispatches the builder.
  The builder reads the review comments, makes changes, pushes to the same branch,
  and transitions back to status:in-review. The cycle repeats up to
  runtime.max_review_cycles (default: 3) times before escalating to status:blocked.

status:blocked
  Human attention required. Agents will not pick up a blocked issue even if
  status:todo is mistakenly re-applied. To unblock: remove status:blocked, resolve
  the underlying problem, then re-apply status:todo.

status:merged
  Set by the watcher when the linked PR is merged. The watcher writes a settlement
  record to .agentOS/logs/ and closes any follow-on:* tasks. No further agent work
  occurs on this issue.

status:closed
  Set by the watcher when the issue is closed without a merged PR (the user manually
  closed it, or it was a duplicate). The watcher writes a settlement record with
  outcome=cancelled.

---

## 3. The Agent Axis

The agent axis records which agent role currently owns the issue. It is informational
and is NOT used as a workflow trigger.

### Labels

    agent:builder     The builder role is currently working on this issue.
    agent:reviewer    The reviewer role is currently evaluating this issue.
    agent:watcher     The watcher is running settlement on this issue.
    agent:docs        The docs agent is updating documentation.
    agent:planner     The planner is writing a plan into the issue body.

### When agent labels are set

The orchestrator sets the appropriate agent:* label at the same time it transitions
the status:* label. For example, when the orchestrator dispatches the builder it
applies agent:builder and removes any prior agent:* label.

### Why agent:* labels do NOT trigger workflows

Workflow triggers that fire on agent:* labels would create a feedback loop: the
orchestrator sets agent:builder, which fires a workflow, which reads agent:builder
and re-dispatches the builder, etc. The status:* axis is the trigger mechanism.
The agent:* axis is purely for human observability — it answers "who is working on
this right now?" without requiring anyone to read the workflow logs.

---

## 4. The Type Axis

The type axis classifies the kind of work an issue represents. Type labels are set
by humans at issue creation time (or via triage automation).

### Labels

    type:feature      New feature or user-facing enhancement.
    type:bug          Defect in existing functionality.
    type:chore        Internal improvement, dependency update, refactoring.
    type:docs         Documentation-only change.
    type:research     Spike or investigation; output is a report, not code.

### Auto-add-to-board behaviour for type:feature

When `type:feature` is applied to an issue (at creation or via label event), the
board agent — if installed — automatically adds the issue to the Agent Board and
sets the Status field to "Todo". This is configured in agentOS.yaml:

    board:
      auto_add:
        - type:feature
        - type:bug

You can add other type labels to `auto_add`. Issues without a type label listed in
`auto_add` are never automatically added; they can still be added manually via the
board UI.

### Restricting automation by type

The orchestrator only dispatches agents for issues that have a type label in the
`routable_types` list (default: feature, bug, chore). Issues labeled type:research
are excluded by default because research spikes do not produce a PR — they produce a
comment or a document.

    runtime:
      routable_types: [feature, bug, chore]

---

## 5. The Review Axis

The review axis carries the reviewer's verdict as sub-flags. These labels are set by
the reviewer agent before it transitions the status:* label, giving the human reader
richer information than the binary approved/changes-requested distinction.

### Labels

    review:lgtm               Code looks good; no concerns.
    review:needs-tests        Logic is correct but test coverage is insufficient.
    review:security-concern   A potential security issue was identified.
    review:scope-violation    The PR changes things outside the issue scope.
    review:spec-question      The implementation contradicts the spec; needs clarification.

### How review:scope-violation works with changes-requested

When the reviewer detects that a PR modifies files or functionality outside what the
issue requested, it applies both `review:scope-violation` and `status:changes-requested`.
It also posts a review comment listing the out-of-scope changes and requesting that
the builder revert or extract them.

The builder, on its next iteration, reads the review comments, identifies the
out-of-scope changes, and either:

a) Reverts them if they were unintentional side-effects of the implementation.
b) Extracts them into a new issue (using the planner role if enabled) and removes
   them from the current PR.

The reviewer clears review:scope-violation only when the PR no longer contains
out-of-scope changes.

### Multiple review labels

A reviewer may apply multiple review:* labels simultaneously. For example:

    review:needs-tests + review:scope-violation

Both concerns must be resolved before the reviewer will transition to status:approved.

---

## 6. The Source Axis

The source axis records how an issue was created. It is set once at creation time
and is never changed.

### Labels

    source:human          Issue was created manually by a human.
    source:agent          Issue was created by an agent (e.g., planner decomposition).
    source:import         Issue was imported from an external system (Jira, Linear, etc.).
    source:webhook        Issue was created by an incoming webhook (e.g., error alert).

### Usage

Source labels are used by the orchestrator to adjust behaviour for agent-created
issues. For example, a source:agent issue created by the planner role is
automatically added to the board and has status:todo applied immediately, without
waiting for human triage. A source:human issue waits for a human to apply status:todo.

This default behaviour is configurable:

    runtime:
      auto_start_agent_issues: true   # default
      auto_start_human_issues: false  # default

---

## 7. The Follow-On Axis

The follow-on axis is used for async handoff: signalling to the watcher that
additional work should happen after the current run completes, without blocking the
current run.

### Labels

    follow-on:needs-docs      Documentation needs updating for this change.
    follow-on:needs-migration A database migration is required.
    follow-on:needs-deploy    A deployment action is queued.

### How routes_to is configured for follow-on

Follow-on labels have their own `routes_to` configuration, but it is handled by the
watcher at settlement time, not by the orchestrator at run time. When the watcher
processes a merged issue and finds a follow-on:* label, it:

1. Creates a new issue linking back to the original.
2. Applies the appropriate type:* and status:todo labels to the new issue.
3. Removes the follow-on:* label from the original issue.

This means the docs update, migration, or deploy runs as a fresh agent job in the
next orchestrator cycle, fully decoupled from the original build-and-review cycle.

Configure which follow-on labels exist and what type they spawn in agentOS.yaml:

    labels:
      follow-on:
        needs-docs:
          routes_to_type: docs
          color: "bfd4f2"
        needs-migration:
          routes_to_type: chore
          color: "bfd4f2"

---

## 8. Adding Custom Labels

### The rule

Plugins may add labels in new axes only. They MUST NOT:

- Modify the colour of any label in the status:*, agent:*, or review:* axes.
- Remove any label that the core spec defines.
- Reuse an existing axis name for a different semantic meaning.

### How to add a custom axis via a plugin

Create a plugin.yaml that declares the new axis:

    name: my-priority-plugin
    version: 1.0.0
    labels:
      priority:
        critical: { color: "b60205", description: "Must be fixed immediately" }
        high:     { color: "e4e669", description: "Fix in current sprint" }
        low:      { color: "cfd3d7", description: "Fix when convenient" }

Reference the plugin from agentOS.yaml:

    plugins:
      - name: my-priority-plugin
        source: local:./plugins/my-priority-plugin

Running `agentOS apply` will create the priority:* labels alongside the core labels.
The core status:*, type:*, agent:*, review:*, source:*, and follow-on:* labels are
unaffected.

### Custom labels and the orchestrator

Custom label axes do not trigger orchestrator routing by default. If you want the
orchestrator to respond to a custom label, you need a custom workflow that listens
for it. See docs/plugins.md for examples.

---

## 9. Label Idempotency — The Upsert Contract

### What upsert means

When `agentOS apply` processes labels it performs an upsert for each label defined
in agentOS.yaml (and active plugins):

- If the label does not exist in the repository: create it.
- If the label exists with a different colour or description: update it.
- If the label exists and is identical: skip it (no API call).

This means running `agentOS apply` multiple times is safe. No labels are deleted
unless you explicitly pass `--prune-labels`, which removes labels present in the
repository but absent from agentOS.yaml.

### What happens when you re-run agentOS apply

On re-run, if no agentOS.yaml changes have been made, the label step typically
outputs:

    Labels: 0 created, 0 updated, 24 skipped

If you added a plugin that declares new labels since the last apply:

    Labels: 3 created, 0 updated, 24 skipped

If you changed a label colour in agentOS.yaml (e.g., to match a rebrand):

    Labels: 0 created, 1 updated, 23 skipped
    updated  status:todo (colour 0075ca -> 1a7ac7)

### Colour immutability for core labels

The core label colours in the status:*, agent:*, and review:* axes are treated as
immutable by the upsert logic unless you pass `--force-colors`. This is intentional:
downstream tooling, dashboards, and human muscle memory rely on colour consistency.
If you need to change a colour, use `--force-colors` and be prepared to update any
tooling that keys on colour.

### Prune behaviour

With `--prune-labels`:

    agentOS apply --repo my-org/my-repo --only labels --prune-labels

Any label present in the repository that is not declared in agentOS.yaml (including
active plugins) will be deleted. Use with caution in repositories with manually
created labels for other purposes (e.g., "good first issue", "help wanted"). You can
exempt labels from pruning:

    labels:
      _prune_exempt:
        - "good first issue"
        - "help wanted"
