# Agent Roles Reference

agentOS uses four core GitHub Apps (builder, reviewer, watcher, board) and two
optional Apps (docs, planner). Each App maps to a distinct agent role with a
distinct permission set, a distinct trigger condition, and a distinct runtime
interface. This document covers all six roles.

---

## 1. The Least-Privilege Model

### Why separate Apps?

A single "agentOS bot" GitHub App with full repository permissions would be simpler
to set up but would violate the least-privilege principle in ways that matter for
autonomous agents:

- An agent that can write code AND approve PRs can approve its own changes. This
  breaks the human-reviewable audit chain.
- An agent that can manage project boards AND push code can hide its own work from
  visibility.
- A single compromised credential gives an attacker full access to every operation
  the agent can perform.

By splitting into separate Apps, each role is constrained to exactly what it needs.
A bug in the reviewer agent cannot push code. A compromised builder credential
cannot approve a PR without a second approval from the reviewer credential, which is
a different private key.

### The approval firewall

The most important boundary is between builder (contents:write) and reviewer
(contents:read, pull_requests:write). This boundary means:

- Only the builder can push code.
- Only the reviewer can approve a PR.
- The builder cannot approve its own PR — it physically cannot call the reviews
  approval API endpoint with its credential because the reviewer App is the one
  installed with pull_requests:write and write_discussion is not granted to builder.

This is enforced by GitHub's permission model, not by agentOS logic. You cannot
accidentally remove this firewall by changing agentOS.yaml; you would need to change
the App permissions on GitHub itself.

### Credential storage

Each App's private key is stored as a GitHub Actions secret. The keys are never
written to the repository filesystem and are not available to other Jobs. Each
workflow job only has access to the specific secret it declares in its `env:` block.

---

## 2. builder

The builder role does the primary implementation work: it reads the issue, writes
code, commits to a branch, and opens a pull request.

### Permission table

    Permission                    Level     Reason
    ----------------------------  --------  ------------------------------------------
    contents                      write     Push commits to feature branches
    pull_requests                 write     Open, update, and close PRs
    issues                        write     Comment, apply/remove labels (status/agent)
    checks                        write     Create check runs to report build status
    actions                       read      Read workflow run logs for context
    statuses                      write     Post commit statuses
    metadata                      read      Required by all Apps (implicit)

### What builder can do

- Create and push to branches matching the convention `agent/issue-<N>-<slug>`.
- Open a pull request from its branch to the base branch (usually main).
- Update the PR description and title.
- Post comments on the issue explaining what was built.
- Remove status:todo and apply status:in-progress at the start of a run.
- Apply status:in-review and agent:reviewer when it opens a PR.
- Apply status:blocked if it cannot complete the task.
- Create check runs on its commits (used to surface linter/test pass/fail).

### What builder cannot do

- Approve pull requests (no pull_requests:write on review endpoints).
- Merge pull requests (merge is a separate permission not granted to any agent; it
  happens via branch protection auto-merge or a human click).
- Modify project board fields (that is the board App's domain).
- Push to the default branch (branch protection rules block direct pushes).
- Write to repository Settings.
- Access secrets (the runner environment has access only to the specific secrets
  declared in the workflow env block).

### Branch convention

All builder branches follow the pattern:

    agent/issue-<issue-number>-<slug>

Where `<slug>` is a URL-safe lowercase version of the issue title, truncated to 40
characters. Examples:

    agent/issue-42-add-hello-world-endpoint
    agent/issue-107-fix-null-pointer-in-auth
    agent/issue-203-update-dependency-flask

The builder always creates a new branch for each run. If a branch already exists
(from a previous run on the same issue), the builder pushes to it (force-push with
lease) rather than creating a new one. This preserves PR history.

### Retry flow

The builder is subject to `runtime.max_review_cycles` (default: 3). The cycle count
is tracked in the issue's comment thread using a special YAML front-matter comment
that the orchestrator reads:

    <!-- agentOS:state
    review_cycle: 2
    last_run_id: run_abc123
    -->

On each changes-requested transition, the orchestrator increments the cycle counter.
When the counter reaches max_review_cycles, the orchestrator applies status:blocked
instead of re-dispatching the builder, and posts a comment explaining that human
intervention is needed.

---

## 3. reviewer

The reviewer role reads the pull request diff and the original issue, evaluates the
implementation, and either approves or requests changes.

### Permission table

    Permission                    Level     Reason
    ----------------------------  --------  ------------------------------------------
    pull_requests                 write     Post review comments, approve, request changes
    issues                        write     Apply/remove labels (status/agent/review axes)
    contents                      read      Read the diff and file contents
    checks                        read      Read CI results before rendering verdict
    metadata                      read      Required by all Apps (implicit)

### Why reviewer intentionally lacks contents:write

The reviewer has `contents: read` only. This is the most important permission
boundary in the entire system. If the reviewer could write to contents, it could:

1. Modify the code it is reviewing (self-referential, defeats the audit).
2. Accept a PR by squashing or amending commits (bypassing branch protection).
3. Create new branches (blurring the builder/reviewer boundary).

The review:* labels and the status transition to approved or changes-requested are
the only outputs the reviewer produces. Everything else is read-only.

### What happens if you give reviewer contents:write

If you modify the reviewer GitHub App to grant contents:write (you can do this in
GitHub App settings), the following bad things become possible:

- The reviewer loop can push "suggestions" directly to the branch. This looks like
  automated pair programming but it creates an unsigned audit trail: did the builder
  write this code, or did the reviewer silently amend it after the fact?
- `agentOS verify` will fail the App permissions check and report a warning:
  "reviewer App has excess permissions: contents:write. This violates the approval
  firewall. See docs/agent-roles.md."
- Future versions of the spec may treat this as a conformance violation.

If you need the reviewer to apply automated fixes (e.g., linter corrections), use
the builder role for that and have the reviewer request changes with a specific
review:* label that the builder knows to respond to.

### Reviewer verdicts

The reviewer must apply at least one review:* label before transitioning status. The
label set signals the reason for the verdict:

    Approved:
      - review:lgtm (must be present for an approval)
      Optional additions: none (approval means all concerns are resolved)

    Changes requested:
      - One or more of: review:needs-tests, review:security-concern,
        review:scope-violation, review:spec-question
      - review:lgtm must NOT be present alongside a changes-requested transition

---

## 4. watcher

The watcher role monitors issue and PR close events, writes settlement records, and
runs scheduled plugins. It is intentionally the most minimal role.

### Permission table

    Permission                    Level     Reason
    ----------------------------  --------  ------------------------------------------
    issues                        write     Apply status:merged/closed, post comments
    pull_requests                 read      Read merged PR metadata for settlement
    actions                       read      Read workflow run logs for settlement data
    metadata                      read      Required by all Apps (implicit)

### Minimal footprint rationale

The watcher runs at the end of every issue lifecycle (on merge or close) and on a
schedule for plugin tasks. Because it runs so frequently and with write access to
issues, a watcher with excess permissions could silently corrupt the label state of
many issues. The minimal permission set means that even if the watcher behaves
incorrectly (due to a bug or a malicious plugin), the blast radius is limited to
issue comments and labels — it cannot push code or approve PRs.

### Settlement use

On the `pull_request.closed` event (with `merged: true`), the watcher:

1. Reads the run record from .agentOS/logs/ (written by the builder during the run).
2. Computes final cost and turn metrics.
3. Writes a settlement record (see docs/metrics-schema.md for the schema).
4. Applies status:merged to the issue.
5. Processes any follow-on:* labels (creates new issues for follow-on work).
6. Closes the run_key in the JSONL log.

On `issues.closed` (without a merged PR):

1. Writes a settlement record with outcome=cancelled.
2. Applies status:closed.
3. Removes any in-flight agent:* labels.

### Scheduled plugin use

Plugins can register scheduled tasks that run via the watcher. These are declared
in plugin.yaml:

    schedules:
      - cron: "0 9 * * 1"    # Monday 09:00 UTC
        run: scripts/weekly-report.sh

The watcher's workflow file (agentOS-watcher.yml) includes a `schedule:` trigger
with the union of all plugin cron expressions. When the schedule fires, the watcher
invokes each plugin's scheduled script in order.

---

## 5. board

The board role manages GitHub Projects (v2): it adds issues to the board, sets field
values (Status, Agent, Sprint, etc.), and removes issues from the board when they
are closed.

### Permission table

    Permission                    Level     Reason
    ----------------------------  --------  ------------------------------------------
    organization_projects         write     Create and modify org-level Projects v2
    repository_projects           write     Create and modify repo-level Projects v2
    issues                        read      Read issue metadata for board field mapping
    metadata                      read      Required by all Apps (implicit)

### Why board is a separate App from watcher

The board App needs `organization_projects:write`. This is a high-privilege scope
that grants write access to all projects in the organisation, not just the ones
agentOS created. Bundling this permission into the watcher would mean that every
watcher operation carries this elevated privilege. By isolating it in the board App,
which only runs on `labeled` events for type:feature and type:bug, the risk surface
is minimised.

Additionally, `organization_projects:write` requires that the GitHub App is
installed at the organisation level (not just the repository level). The board App
installation flow handles this explicitly; the other three core Apps are installed
at the repository level only.

### organization_projects vs repository_projects gotcha

GitHub Projects (v2) boards can be organisation-level or repository-level.
agentOS creates organisation-level boards by default because they can contain issues
from multiple repositories (useful for multi-repo organisations). This requires the
board App to be installed at the org level.

If you are working in a personal GitHub account (no organisation), you must use
repository-level projects:

    board:
      scope: repository    # default: organization

Failing to set this correctly causes the board App to receive 403 errors when trying
to create the board, because a personal account does not have an organisation-level
projects API. The error looks like:

    GraphQL error: Must have admin rights to Repository.

Fix: set `board.scope: repository` in agentOS.yaml and re-run `agentOS apply --only board`.

---

## 6. docs (optional)

The docs role updates documentation files (README, API references, changelog) after
a feature is approved. It is disabled by default.

### When to enable

Enable the docs role when:

- Your project has structured documentation that should track code changes.
- You want auto-generated API references or changelogs.
- You want the README's feature table updated when new features ship.

### Permission reuse

The docs role reuses the builder GitHub App. It is not a separate App. This means
the docs role runs with builder permissions (contents:write, pull_requests:write,
issues:write). The docs "role" is simply a separate workflow and a separate runner
invocation with a docs-specific prompt.

Enable it in agentOS.yaml:

    agents:
      docs:
        enabled: true
        trigger: status:approved
        prompt: .agentOS/prompts/docs.md

### Trigger

The docs workflow fires when status:approved is applied to an issue. It creates a
branch `agent/docs-issue-<N>`, updates the relevant documentation files, and opens
a PR targeting the same base branch as the feature PR.

### Interaction with the feature PR

The docs PR is separate from the feature PR. Both can be merged independently. The
docs PR includes a reference to the feature issue and PR in its description. If the
feature PR is reverted after the docs PR is merged, the docs must be manually
reverted as well — agentOS does not currently handle this cascading revert scenario.

---

## 7. planner (optional)

The planner role decomposes large issues into smaller sub-issues. It is disabled by
default.

### When to enable

Enable the planner role when:

- Issues frequently require breaking down into multiple PRs.
- You want the agent to automatically identify scope and create sub-tasks.
- You have a project manager who creates high-level issues that need decomposition.

### Permission reuse

Like the docs role, the planner reuses the builder GitHub App. It has the same
permission set.

Enable it in agentOS.yaml:

    agents:
      planner:
        enabled: true
        trigger_label: "needs:planning"
        max_sub_issues: 8
        prompt: .agentOS/prompts/planner.md

### Trigger

The planner fires when the label `needs:planning` is applied. This label is not in
the core label model; you must add it either via plugin or by adding it to your
agentOS.yaml labels section. The planner creates sub-issues with source:agent and
immediately applies status:todo to each, which starts the builder cycle for each
sub-issue.

---

## 8. The Runtime Interface

### Environment variables injected

When the orchestrator dispatches an agent, it invokes the runner command with the
following environment variables set:

    AGENTOS_REPO          owner/repo (e.g., my-org/my-repo)
    AGENTOS_ISSUE_NUMBER  The issue number being worked on (integer as string)
    AGENTOS_ROLE          The agent role: builder, reviewer, watcher, docs, planner
    AGENTOS_RUN_ID        A unique identifier for this run (UUID v4)
    AGENTOS_BRANCH        The branch name to create/use (builder and docs only)
    AGENTOS_BASE_BRANCH   The base branch to target for the PR
    AGENTOS_MAX_TURNS     Maximum number of LLM turns allowed
    AGENTOS_TIMEOUT       Maximum wall-clock seconds allowed
    GITHUB_TOKEN          The GitHub App installation token (short-lived, 1 hour)
    GITHUB_REPOSITORY     owner/repo (standard GitHub Actions variable)
    GITHUB_WORKSPACE      Path to the checked-out repository

LLM provider API keys are available if they were set as repository secrets and
declared in the workflow env block. They do not have a fixed AGENTOS_ prefix; use
the name you gave the secret (e.g., ANTHROPIC_API_KEY).

### Exit code contract

    0     Success. The run completed normally.
    1     Failure. The run could not complete; status:blocked will be applied.
    2     Partial. The run made progress but requires human input. status:blocked.
    3     Retry. Transient error; the orchestrator will retry (up to max_retries).

Any exit code other than 0 or 3 is treated as 1 (failure). The orchestrator reads
the exit code from the workflow job result.

### Branch naming

Runners must respect the AGENTOS_BRANCH environment variable for the branch name.
Runners that create their own branches (ignoring AGENTOS_BRANCH) will cause verify
to fail because the branch name check in post-run validation expects the agentOS
convention.

---

## 9. Swapping the Runner

The `runtime.runner` value in agentOS.yaml controls which command is invoked. The
orchestrator passes AGENTOS_* environment variables to whatever process is invoked.

### Built-in runner: hermes

    runtime:
      runner: hermes

Invokes `hermes run` with the issue number and role. Hermes reads
.agentOS/hermes-config.yaml for model selection, max turns, and tool configuration.

    hermes run \
      --issue $AGENTOS_ISSUE_NUMBER \
      --role $AGENTOS_ROLE \
      --config .agentOS/hermes-config.yaml

### Built-in runner: claude

    runtime:
      runner: claude

Invokes `claude` (Anthropic's Claude CLI) with a constructed prompt that includes
the issue body and the role-specific instructions from .agentOS/prompts/<role>.md.

    claude --prompt "$(cat .agentOS/prompts/$AGENTOS_ROLE.md)" \
           --context "Issue #$AGENTOS_ISSUE_NUMBER"

Requires ANTHROPIC_API_KEY to be set.

### Built-in runner: codex

    runtime:
      runner: codex

Invokes OpenAI Codex CLI. Requires OPENAI_API_KEY.

    codex --model gpt-4o \
          --instructions "$(cat .agentOS/prompts/$AGENTOS_ROLE.md)"

### Custom runner

    runtime:
      runner: custom
      runner_command: "python3 .agentOS/my_runner.py"

The custom runner command is invoked as a shell command. All AGENTOS_* environment
variables are available. The command must honour the exit code contract above.

Example custom runner for a multi-model setup where the builder uses Claude and the
reviewer uses GPT-4o:

    #!/usr/bin/env python3
    # .agentOS/my_runner.py
    import os, subprocess, sys

    role = os.environ["AGENTOS_ROLE"]
    if role == "builder":
        cmd = ["claude", "--model", "claude-opus-4-5", ...]
    elif role == "reviewer":
        cmd = ["openai", "run", "--model", "gpt-4o", ...]
    else:
        cmd = ["hermes", "run", ...]

    result = subprocess.run(cmd)
    sys.exit(result.returncode)

### Per-role runner override

You can override the runner for a specific role without changing the global default:

    runtime:
      runner: hermes
    agents:
      reviewer:
        runner: claude    # reviewer uses Claude even though builder uses hermes

This is useful when you want a faster/cheaper model for the builder and a more
careful model for the reviewer.
