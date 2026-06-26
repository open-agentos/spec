# Plugin Development Guide

Plugins are the extension mechanism for agentOS. They allow teams to add
project-specific labels, board fields, workflows, and agent configuration without
modifying the core spec — and without forking.

---

## 1. Why Plugins Exist

### Core is minimal and stable

The agentOS core spec defines the minimum required label model, board structure, and
workflow triggers needed for the builder/reviewer/watcher lifecycle to function.
It is intentionally narrow. The core changes slowly, with semver versioning and
migration guides for breaking changes.

Domain-specific requirements vary enormously across projects:

- A SaaS product team wants priority:* labels and sprint tracking.
- A data pipeline team wants data-quality:* labels and dbt run triggers.
- An open-source project wants contribution:* labels and a CLA-check workflow.

If each of these were added to the core spec, the core would become bloated, and
every user would need to understand and configure features they do not use.

### Plugins keep core upgrades safe

When you upgrade to a new spec version (`agentOS init --from github:open-agentos/spec@v1.5.0 --merge`),
only the core-managed sections of agentOS.yaml are updated. Plugin-managed sections
are preserved. Your custom labels, workflows, and board fields survive the upgrade
untouched.

### Plugins are composable

You can use multiple plugins simultaneously. A project might use:

    plugins:
      - name: priority
        source: github:my-org/agentos-plugin-priority@v1.2.0
      - name: three-questions
        source: local:./plugins/three-questions
      - name: dbt-integration
        source: github:data-team/agentos-plugin-dbt@main

Each plugin is applied in order. If two plugins would conflict (e.g., both define a
label with the same name but different colours), the later plugin wins and a warning
is printed.

---

## 2. Plugin Manifest (plugin.yaml)

Every plugin directory must contain a plugin.yaml file at its root. This is the
manifest that agentOS reads to understand what the plugin contributes.

### Full field reference

    # plugin.yaml

    name: my-plugin               # Required. Unique identifier. Lowercase, hyphens ok.
    version: "1.0.0"              # Required. Semver string.
    description: "Short description of what this plugin does."   # Required.
    author: "Team Name <team@example.com>"    # Optional.
    requires_spec: ">=1.3.0"      # Optional. Minimum agentOS spec version required.

    # Labels this plugin contributes. Must use a new axis name (prefix).
    # Do NOT use status:, agent:, type:, review:, source:, or follow-on: here.
    labels:
      priority:
        critical:
          color: "b60205"
          description: "Must be fixed in the current sprint"
        high:
          color: "e4e669"
          description: "Should be fixed in the current sprint"
        medium:
          color: "0075ca"
          description: "Fix when bandwidth allows"
        low:
          color: "cfd3d7"
          description: "Nice to have"

    # Board fields this plugin adds to the Agent Board.
    board_fields:
      - name: Priority
        type: single_select
        options: [Critical, High, Medium, Low]
      - name: Sprint
        type: iteration
        duration_days: 14

    # Workflow files this plugin contributes.
    # These are copied into .github/workflows/ during agentOS apply.
    workflows:
      - src: workflows/priority-escalation.yml
        dst: agentOS-plugin-priority-escalation.yml

    # Agent config overrides or additions for specific roles.
    agent_config:
      builder:
        extra_context_files:
          - PRIORITY_GUIDELINES.md
      reviewer:
        extra_prompt_sections:
          - prompts/review-priority-check.md

    # Scheduled tasks (run by the watcher on a cron schedule).
    schedules:
      - name: weekly-priority-review
        cron: "0 9 * * 1"
        run: scripts/weekly-priority-review.sh
        description: "Posts a weekly summary of unresolved critical issues"

    # Permissions required by this plugin's workflows.
    # agentOS verify checks that these are satisfied.
    required_permissions:
      issues: write
      pull_requests: read

### Field descriptions

- `name`: Must be unique across all active plugins. Used as the key in agentOS.yaml's
  `plugins` list.
- `version`: Used to detect when a plugin has been upgraded. agentOS stores the last
  applied version and warns when it changes.
- `requires_spec`: If the user's agentOS.yaml spec version is lower than this, agentOS
  apply will refuse to apply the plugin and print an upgrade instruction.
- `labels`: See Section 3. Must not reuse core axis names.
- `board_fields`: Fields added to the board defined in agentOS.yaml's `board` section.
  The board must already exist (agentOS apply --only board must have been run).
- `workflows`: Each entry copies a workflow file from the plugin directory into
  .github/workflows/. The `dst` filename must be prefixed with `agentOS-plugin-` to
  avoid conflicts with core workflow files.
- `agent_config`: Adds context files or prompt sections to role-specific agent
  configurations. These are merged with (not replacing) the base agent config.
- `schedules`: Scheduled scripts are invoked by the watcher workflow. The script
  must be executable and return exit code 0 on success.

---

## 3. What Plugins Can Add

### Labels (new axes only)

Plugins may define any number of new label axes. An axis is the prefix before the
colon (e.g., `priority`, `sprint`, `compliance`). The label name takes the form
`<axis>:<value>`.

Valid:

    labels:
      priority:
        high: { color: "e4e669", description: "..." }

Invalid (reuses core axis):

    labels:
      status:
        triaged: { color: "...", description: "..." }   # FORBIDDEN — status: is a core axis

### Board fields

Plugins can add fields to the existing Agent Board. Supported field types:

    single_select     Dropdown with defined options.
    text              Free-text field.
    number            Numeric field.
    date              Date picker.
    iteration         GitHub Projects iteration (sprint-style).

### Workflows

Plugins can add GitHub Actions workflow files. These workflows can trigger on any
GitHub event, including label events for the plugin's own label axes. They run with
the default GITHUB_TOKEN unless they declare specific App credentials.

### Agent config

Plugins can add:
- `extra_context_files`: Files that are prepended to the agent's context window
  before each run. Use for project guidelines, style guides, or architectural notes.
- `extra_prompt_sections`: Markdown files that are appended to the role-specific
  system prompt. Use to add role-specific instructions related to the plugin's domain.

---

## 4. What Plugins MUST NOT Do

### Do not modify core label colours

The colours of status:*, agent:*, review:*, type:*, source:*, and follow-on:* labels
are fixed by the core spec. Changing them via a plugin breaks downstream tooling,
dashboards, and human visual expectations.

If you attempt to declare a core label in your plugin.yaml, agentOS apply will
print a warning and skip that label:

    [WARN] plugin 'my-plugin' declares label 'status:todo' which is owned by core. Skipping.

### Do not remove core fields from the board

Plugins must not declare negative board field overrides. The board fields defined by
the core spec (Status, Agent) must always be present.

### Do not conflict with spec conformance

Spec conformance (`agentOS verify --conformance`) checks that the repository has all
required labels, fields, and workflows in their canonical form. A plugin that renames
or removes core-required items causes conformance to fail. If you need to change a
core item's behaviour, discuss it in the open-agentos/spec issue tracker as a spec
proposal.

### Do not use the agentOS-core- workflow name prefix

Workflow filenames beginning with `agentOS-` (without the `plugin-` infix) are
reserved for core workflows. Plugin workflows must use the `agentOS-plugin-` prefix:

    Good:  agentOS-plugin-priority-escalation.yml
    Bad:   agentOS-orchestrator.yml     (shadows a core workflow)
    Bad:   agentOS-priority.yml         (missing plugin- infix)

---

## 5. Writing Your First Plugin

This walkthrough creates a minimal plugin that adds a `priority` axis to the label
model and a board field for priority tracking.

### Step 1: Create the plugin directory

    mkdir -p plugins/priority
    cd plugins/priority

### Step 2: Write plugin.yaml

    cat > plugin.yaml << 'EOF'
    name: priority
    version: "1.0.0"
    description: "Adds priority:* labels and a Priority board field."
    author: "Your Name <you@example.com>"
    requires_spec: ">=1.3.0"

    labels:
      priority:
        critical: { color: "b60205", description: "Fix immediately" }
        high:     { color: "e4e669", description: "Fix this sprint" }
        medium:   { color: "0075ca", description: "Fix soon" }
        low:      { color: "cfd3d7", description: "Fix when convenient" }

    board_fields:
      - name: Priority
        type: single_select
        options: [Critical, High, Medium, Low]
    EOF

### Step 3: Test with --dry-run

    cd ../..    # back to repository root
    agentOS apply --repo my-org/my-repo --dry-run --plugin plugins/priority

Dry-run output shows exactly what would be created without touching GitHub:

    [DRY RUN] Would create label: priority:critical (#b60205)
    [DRY RUN] Would create label: priority:high (#e4e669)
    [DRY RUN] Would create label: priority:medium (#0075ca)
    [DRY RUN] Would create label: priority:low (#cfd3d7)
    [DRY RUN] Would add board field: Priority (single_select)

### Step 4: Reference the plugin in agentOS.yaml

    plugins:
      - name: priority
        source: local:./plugins/priority

### Step 5: Apply

    agentOS apply --repo my-org/my-repo

The apply output now includes the plugin's contributions:

    [1/4] Syncing labels ...
    created  priority:critical
    created  priority:high
    created  priority:medium
    created  priority:low
    Labels: 4 created (plugin: priority), 24 skipped (core)

    [2/4] Provisioning project board ...
    added field: Priority (single_select)

### Step 6: Verify

    agentOS verify --repo my-org/my-repo

The verify output includes plugin checks:

    [PASS] Plugin: priority    4/4 labels, 1/1 board fields

---

## 6. The three-questions Reference Plugin

The `three-questions` plugin ships inside the agentOS-spec repository as a fully
annotated reference implementation. It adds a pre-flight checklist to the builder
workflow: before writing any code, the agent must answer three scoping questions.

Location: `plugins/three-questions/`

### Directory structure

    plugins/three-questions/
      plugin.yaml
      prompts/
        builder-prefix.md       # Prepended to the builder system prompt
      workflows/
        agentOS-plugin-three-questions-check.yml   # Optional: posts the answers as a PR comment
      scripts/
        validate-answers.py     # Called by the workflow to check answer completeness

### Annotated plugin.yaml

    name: three-questions
    version: "2.1.0"
    description: |
      Requires the builder agent to answer three scoping questions before writing code:
      1. What exactly is in scope for this issue?
      2. What is explicitly out of scope?
      3. What is the acceptance criterion?
      The answers are posted as a PR comment for human review.

    requires_spec: ">=1.3.0"

    # No new labels — this plugin works entirely through prompt injection.
    labels: {}

    # No new board fields.
    board_fields: []

    # Adds a workflow that reads the PR description and validates that the
    # three-questions section is present and complete.
    workflows:
      - src: workflows/agentOS-plugin-three-questions-check.yml
        dst: agentOS-plugin-three-questions-check.yml

    # Injects the three-questions prompt into the builder's context.
    agent_config:
      builder:
        extra_prompt_sections:
          - prompts/builder-prefix.md

### Annotated builder-prefix.md

    # Three Questions (required)

    Before writing any code, you MUST answer the following three questions in a
    structured block at the start of your first message.

    Format your answers exactly like this:

    <!-- three-questions
    in_scope: |
      <bullet list of what you will change>
    out_of_scope: |
      <bullet list of what you will NOT change, even if related>
    acceptance_criterion: |
      <one sentence: what must be true for this issue to be considered done>
    -->

    Do not skip this step. The validate-answers.py script checks that this block is
    present in your first PR comment. If it is absent or incomplete, the PR check
    will fail.

### What the workflow does

The `agentOS-plugin-three-questions-check.yml` workflow fires on `pull_request.opened`
and `pull_request.synchronize`. It:

1. Reads the first comment on the PR (posted by the builder).
2. Runs `scripts/validate-answers.py` which checks for the `<!-- three-questions ... -->`
   block and validates that none of the fields are empty.
3. Posts a GitHub check run with pass/fail status.
4. If the check fails, it posts a comment requesting the builder add the answers.

This workflow uses only the default GITHUB_TOKEN (pull_requests:write) — it does not
require any agentOS App credentials.

---

## 7. Referencing a Plugin from agentOS.yaml

### Local source format

Use a relative path from the agentOS.yaml file:

    plugins:
      - name: priority
        source: local:./plugins/priority

The path must point to a directory containing plugin.yaml. Relative paths are
resolved relative to the location of agentOS.yaml (typically the repository root).

### GitHub source format

Use the `github:` prefix with owner/repo@ref:

    plugins:
      - name: priority
        source: github:my-org/agentos-plugin-priority@v1.2.0

    plugins:
      - name: dbt
        source: github:data-team/agentos-plugin-dbt@main

The `@ref` component can be a tag, branch name, or full commit SHA. For production
use, always pin to a tag or commit SHA rather than a branch name, so plugin updates
do not unexpectedly change your repository's behaviour.

When agentOS apply runs with a github: source, it fetches the plugin repository at
the specified ref and caches it in .agentOS/plugin-cache/. The cache is keyed on
owner/repo@SHA (the resolved SHA, even if you specified a branch name) so re-running
apply with the same branch name is idempotent as long as the ref has not moved.

### Mixing local and GitHub sources

You can mix both in the same plugins list:

    plugins:
      - name: three-questions
        source: local:./plugins/three-questions       # developed in-repo
      - name: priority
        source: github:my-org/agentos-plugin-priority@v1.2.0   # shared plugin
      - name: custom-workflow
        source: local:./plugins/custom-workflow       # repo-specific
      - name: sla-tracking
        source: github:ops-team/agentos-plugin-sla@v2.0.0      # team plugin

---

## 8. Plugin Loading Order

Plugins are applied after core, in the order they appear in the plugins list.

### Why order matters

If two plugins both try to configure the same resource:

- Two plugins declaring `board_fields` with a field named "Assignee" will result in
  the second plugin's definition winning (with a warning).
- Two plugins declaring labels in the same axis (e.g., both declare a `priority:`
  axis) will have their labels merged, with the second plugin's colour winning for
  any name collision.

To avoid conflicts, use distinct axis names and distinct board field names across
plugins. If you encounter a conflict between a community plugin and a local plugin,
put your local plugin last in the list so it wins.

### Core always loads first

Core labels, board fields, and workflows are applied before any plugin. You cannot
override a core item via a plugin. If you declare a label that clashes with a core
label in your plugin.yaml, it is ignored with a warning:

    [WARN] plugin 'my-plugin' at position 1 declares 'status:todo' (core-owned). Skipped.

### Viewing load order

    agentOS apply --dry-run --verbose

With `--verbose`, the dry-run output shows the load order explicitly:

    Load order:
      [0] core (spec v1.4.0)
      [1] three-questions (v2.1.0, local:./plugins/three-questions)
      [2] priority (v1.2.0, github:my-org/agentos-plugin-priority@v1.2.0)

---

## 9. Publishing a Plugin

### It is just a public GitHub repo

A plugin is a public (or private, for internal use) GitHub repository with a
plugin.yaml at its root. No registry, no package manager, no submission process.

### Recommended repository structure

    agentos-plugin-priority/
      plugin.yaml            # Manifest (required)
      README.md              # Usage instructions
      CHANGELOG.md           # Version history
      prompts/               # Prompt injection files (if any)
      workflows/             # Workflow files (if any)
      scripts/               # Scripts called by workflows (if any)
      tests/                 # Test fixtures and test script (see Section 10)

### Versioning with git tags

Use semantic versioning tags:

    git tag v1.0.0
    git push origin v1.0.0

When users reference your plugin with `@v1.0.0`, they get a stable, immutable
version. When you release a new version:

1. Update `version:` in plugin.yaml to match the new tag.
2. Update CHANGELOG.md.
3. Push the tag.

agentOS will detect that the plugin version changed the next time `agentOS apply`
runs and will print:

    [INFO] plugin 'priority' updated from v1.0.0 to v1.2.0. Run agentOS apply to sync.

### Discoverability

Add the topic `agentos-plugin` to your GitHub repository to make it discoverable:

    gh repo edit my-org/agentos-plugin-priority --add-topic agentos-plugin

Users can find community plugins by searching GitHub for the topic.

---

## 10. Testing a Plugin

### Dry-run with a plugin enabled

The primary testing tool is `agentOS apply --dry-run`:

    agentOS apply \
      --repo my-org/test-repo \
      --dry-run \
      --plugin ./plugins/my-plugin

Dry-run validates:

- plugin.yaml is syntactically valid.
- All required fields are present.
- No core axes are redeclared.
- No workflow filenames conflict with core or other active plugins.
- No board_fields conflict with existing fields.

If any validation fails, agentOS prints an error and exits 1 without touching GitHub.

### Full integration test

For a complete integration test, use a dedicated test repository:

    # Create a clean test repo
    gh repo create my-org/agentos-plugin-test --private --clone
    cd agentos-plugin-test

    # Init spec
    agentOS init --from github:open-agentos/spec@main

    # Apply with your plugin
    agentOS apply \
      --repo my-org/agentos-plugin-test \
      --plugin /path/to/your-plugin

    # Verify everything passed
    agentOS verify --repo my-org/agentos-plugin-test

    # Clean up
    gh repo delete my-org/agentos-plugin-test --yes

### Automated plugin tests

For plugins that include scripts or workflows, write a tests/run-tests.sh script:

    #!/usr/bin/env bash
    set -euo pipefail

    # Test that plugin.yaml is valid
    agentOS plugin validate ./plugin.yaml

    # Test dry-run against a fixture agentOS.yaml
    agentOS apply \
      --config tests/fixtures/agentOS.yaml \
      --plugin . \
      --dry-run \
      --output-json /tmp/dry-run-output.json

    # Assert expected labels are present in dry-run output
    python3 tests/assert-dry-run.py /tmp/dry-run-output.json

Run this in CI using a GitHub Actions workflow in your plugin repository:

    # .github/workflows/test.yml
    on: [push, pull_request]
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - run: pip install agentOS-cli
          - run: bash tests/run-tests.sh

### The plugin validate subcommand

    agentOS plugin validate ./plugins/my-plugin

Validates plugin.yaml without running apply. Useful in pre-commit hooks:

    # .pre-commit-config.yaml
    repos:
      - repo: local
        hooks:
          - id: validate-plugins
            name: Validate agentOS plugins
            entry: agentOS plugin validate
            language: system
            files: plugins/.*/plugin\.yaml
            pass_filenames: true
