# Getting Started with agentOS

This walkthrough takes roughly 30 minutes end-to-end. By the end you will have a
GitHub repository fully provisioned with the agentOS label model, project board,
GitHub Actions workflows, and agent scaffolding — and you will have watched your
first automated agent run complete successfully.

---

## 1. Prerequisites

Before you begin, confirm that every item in this list is in place. Skipping any of
them causes hard-to-diagnose failures later.

### GitHub account and gh CLI

You need a GitHub account with permission to create repositories and GitHub Apps
within an organisation (or your personal namespace). The `gh` CLI must be installed
and authenticated:

    gh auth status

Expected output includes "Logged in to github.com as <your-username>". If not, run:

    gh auth login

Choose HTTPS, authenticate via browser, and grant the scopes that gh requests. For
organisation use you also need the `admin:org` and `project` OAuth scopes:

    gh auth refresh -s admin:org,project

### Python 3.11 or newer

agentOS tooling requires Python 3.11+. Check your version:

    python3 --version

If you are on an older Python, use pyenv or your OS package manager to install a
newer version before continuing.

### uv (recommended package manager)

uv is the recommended way to install and run agentOS CLI tools because it manages
isolated environments automatically and resolves dependencies far faster than pip.

    curl -LsSf https://astral.sh/uv/install.sh | sh

Verify:

    uv --version

pip works too (see Section 2), but the rest of this guide assumes uv.

### An LLM provider API key

agentOS orchestrates agents that call an LLM provider. You need at least one of:

    ANTHROPIC_API_KEY     — for Claude models (default runner in most configs)
    OPENAI_API_KEY        — for GPT-4o and o1 models
    OPENROUTER_API_KEY    — for any model via OpenRouter

Export the key you plan to use so the CLI can validate it during setup:

    export ANTHROPIC_API_KEY=sk-ant-...

You will also store it as a GitHub Actions secret in Section 6.

---

## 2. Install the CLI

Install agentOS CLI into an isolated environment using uv:

    uv tool install agentOS-cli

This installs the `agentOS` command globally in your uv tools path. Confirm:

    agentOS --version
    # agentOS-cli 1.x.y

If you prefer plain pip (inside a virtual environment):

    python3 -m venv .venv
    source .venv/bin/activate
    pip install agentOS-cli
    agentOS --version

Either way, the `agentOS` binary must be on your PATH for the rest of this guide.

---

## 3. Create a Target Repository

You need a GitHub repository to provision. You can use an existing repo or create a
new one. For this walkthrough we create a fresh one so nothing pre-existing
interferes.

Create via gh CLI:

    gh repo create my-org/my-agent-repo \
      --private \
      --description "agentOS-powered feature delivery" \
      --clone

    cd my-agent-repo

This clones the repository locally. agentOS commands that take `--repo` accept the
`owner/repo` form and operate both locally and via the GitHub API.

Using an existing repository is fine. Just ensure you have admin access (Settings
tab visible) because the setup step in Section 6 registers GitHub Apps that require
admin-level repo and org permissions.

---

## 4. Initialise the Spec

From inside the repository directory, run:

    agentOS init --from github:open-agentos/spec@main

This command:

1. Fetches the canonical agentOS.yaml specification from the open-agentos/spec
   repository at the `main` branch.
2. Writes agentOS.yaml to the current directory.
3. Writes a minimal .agentOS/ directory scaffold (keys/, logs/, plugins/ stubs).
4. Adds .agentOS/keys/ to .gitignore so credential files are never committed.

You should see output like:

    Fetching spec from github:open-agentos/spec@main ... done
    Writing agentOS.yaml ... done
    Writing .agentOS/ scaffold ... done
    Hint: review agentOS.yaml, then run: agentOS setup --repo owner/my-repo --org my-org

Commit the initial files:

    git add agentOS.yaml .agentOS/ .gitignore
    git commit -m "chore: initialise agentOS spec"
    git push

To pin to a specific spec version instead of main, use a tag:

    agentOS init --from github:open-agentos/spec@v1.4.0

Pinning is strongly recommended for production repositories so a spec update does
not unexpectedly change your label model or workflow behaviour.

---

## 5. Review agentOS.yaml

Open agentOS.yaml in your editor. The file is heavily commented, but here are the
four sections you should understand before continuing.

### runtime.runner

    runtime:
      runner: hermes          # which agent executable runs in CI
      runner_image: ""        # optional Docker image; empty = use Actions runner default
      timeout_minutes: 30
      max_turns: 50

`runner` controls which command is invoked when the orchestrator workflow fires.
Built-in values are `hermes`, `claude`, `codex`, and `custom`. For custom runners
see docs/agent-roles.md. Change this to match the LLM tool you have installed.

### labels

    labels:
      status:
        todo:      { color: "0075ca", description: "Ready to be picked up" }
        in-progress: ...
      type:
        feature:   { color: "a2eeef", description: "New feature or request" }
        ...

The labels section declares the full label model that `agentOS apply` will create in
your repository. Do not modify colour values in the status or agent axes unless you
are prepared to update all downstream workflow filters. See docs/label-model.md for
the full axis reference.

### board

    board:
      name: "Agent Board"
      fields:
        - name: Status
          type: single_select
          options: [Todo, In Progress, In Review, Done, Blocked]
        - name: Agent
          type: text

`board` configures the GitHub Projects (v2) board that `agentOS apply` creates. The
`board_id` field is intentionally left blank in the initial file — it gets populated
automatically after `agentOS apply` runs. Do not set it by hand.

### plugins

    plugins: []

Plugins extend the core spec with domain-specific labels, workflows, or agent
config. You add them here after the core provisioning is complete. See docs/plugins.md
for the full guide. For now, leave this empty.

---

## 6. Register GitHub Apps

agentOS uses four GitHub Apps (one per agent role) instead of a single token. This
enforces least-privilege: each App has only the permissions its role needs. You
create all four in one command:

    agentOS setup --repo my-org/my-agent-repo --org my-org

### The browser flow

The CLI opens your browser four times in sequence (or once per App if you pass
`--apps builder,reviewer,watcher,board`). Each browser session lands on the GitHub
App creation page with all fields pre-filled. You must:

1. Review the permission list shown on the page.
2. Click "Create GitHub App".
3. On the next page, click "Generate a private key" — this downloads a .pem file.
4. Return to the terminal and press Enter when prompted.

The CLI waits for you to press Enter between each App creation. Do not rush through
the browser steps; if you skip "Generate a private key" you will need to regenerate
it manually in GitHub Settings.

### The four Apps created

    builder   — builds code, opens PRs, pushes branches
                Permissions: contents:write, pull_requests:write, issues:write,
                             checks:write, actions:read

    reviewer  — reads PRs, posts review comments, approves or requests changes
                Permissions: pull_requests:write, issues:write, contents:read

    watcher   — monitors issue/PR events, writes settlement records, runs scheduled plugins
                Permissions: issues:write, pull_requests:read, actions:read

    board     — manages GitHub Projects (v2) board fields and item status
                Permissions: organization_projects:write, repository_projects:write

### Where credentials go

After you press Enter for each App, the CLI:

1. Reads the downloaded .pem from your ~/Downloads directory (or the path you
   specified with --key-dir).
2. Writes it to .agentOS/keys/<role>.pem.
3. Records the App ID in agentOS.yaml under github_apps.<role>.app_id.
4. Stores the App installation ID after installing the App on your repository
   (this also happens in the browser; the CLI detects it via the API).

The .pem files never leave your machine except when uploaded as GitHub Actions
secrets in the next step. After `agentOS apply` they are uploaded automatically.
Never commit them.

---

## 7. Provision

With Apps registered, provision the repository:

    agentOS apply --repo my-org/my-agent-repo

This command runs four sequential steps. You will see each step's progress in the
terminal.

### Step 1: Labels

    [1/4] Syncing labels ...

The CLI reads the `labels` section of agentOS.yaml and upserts every label in the
repository. "Upsert" means: create if missing, update colour/description if they
differ, skip if identical. Labels that exist in the repo but are not in agentOS.yaml
are left alone (no deletions unless you pass `--prune-labels`).

Typical output:

    created  status:todo
    created  status:in-progress
    created  status:in-review
    created  status:approved
    created  status:changes-requested
    created  status:blocked
    created  status:merged
    created  status:closed
    created  type:feature
    ...
    Labels: 24 created, 0 updated, 0 skipped

### Step 2: Board

    [2/4] Provisioning project board ...

Creates the GitHub Projects (v2) board defined in the `board` section, adds all
custom fields, and writes the board_id back into agentOS.yaml. The updated
agentOS.yaml is committed to your repository automatically:

    board_id: PVT_kwDOB...

### Step 3: Workflows

    [3/4] Writing GitHub Actions workflows ...

Writes .github/workflows/ files for each enabled agent role:

    .github/workflows/agentOS-orchestrator.yml
    .github/workflows/agentOS-builder.yml
    .github/workflows/agentOS-reviewer.yml
    .github/workflows/agentOS-watcher.yml
    .github/workflows/agentOS-board.yml

These files reference the App credentials via repository secrets. The CLI also
uploads the .pem files and App IDs as GitHub Actions secrets at this step:

    Uploading secret AGENTOS_BUILDER_APP_ID ... done
    Uploading secret AGENTOS_BUILDER_PRIVATE_KEY ... done
    ... (repeated for each role)

### Step 4: Scaffold

    [4/4] Writing agent scaffold ...

Writes any runner-specific scaffold files. For the `hermes` runner this means:

    .agentOS/hermes-config.yaml
    .agentOS/prompts/builder.md
    .agentOS/prompts/reviewer.md

These are starting-point files — edit them to tune agent behaviour for your project.

After all four steps complete:

    Apply complete.
    Repository:  my-org/my-agent-repo
    Labels:      24 created
    Board:       Agent Board (PVT_kwDOB...)
    Workflows:   5 written
    Scaffold:    3 files written
    Next step:   agentOS verify --repo my-org/my-agent-repo

Commit the generated files:

    git add agentOS.yaml .github/ .agentOS/
    git commit -m "chore: agentOS apply — provision labels, board, workflows"
    git push

---

## 8. Verify

Run the verification check:

    agentOS verify --repo my-org/my-agent-repo

This check connects to GitHub and validates:

- All required labels exist with the correct colours
- The project board exists and has all required fields
- All five workflow files are present in the default branch
- All required GitHub Actions secrets are set (non-empty; the values are not read)
- The four GitHub Apps are installed on the repository
- agentOS.yaml contains a non-empty board_id

### Passing result

    Verifying my-org/my-agent-repo ...

    [PASS] Labels         24/24 required labels present
    [PASS] Board          "Agent Board" found, 5 fields verified
    [PASS] Workflows      5/5 workflow files present
    [PASS] Secrets        8/8 required secrets set
    [PASS] Apps           4/4 Apps installed
    [PASS] Config         board_id set, runner=hermes

    All checks passed. Your repository is ready for agentOS.

If any check shows [FAIL], the message explains exactly what is missing. The most
common failures are covered in Section 10.

---

## 9. Fire the First Agent Run

The orchestrator workflow triggers when an issue has both a `type:*` label and the
`status:todo` label simultaneously. Here is how to fire the first run:

### Create a type:feature issue

Via the GitHub UI:

1. Open your repository on github.com.
2. Click Issues -> New issue.
3. Title: "Add hello-world endpoint"
4. Body: describe what you want built (a few sentences is enough).
5. Apply the label `type:feature` using the Labels dropdown.
6. Submit the issue.

Via gh CLI:

    gh issue create \
      --title "Add hello-world endpoint" \
      --body "Create a GET /hello endpoint that returns {\"message\": \"hello world\"}" \
      --label "type:feature" \
      --repo my-org/my-agent-repo

### Apply status:todo to trigger the orchestrator

    gh issue edit <issue-number> --add-label "status:todo" --repo my-org/my-agent-repo

The `agentOS-orchestrator.yml` workflow fires on the `labeled` event filtered to
`status:todo`. Within a few seconds you should see a workflow run appear under
Actions.

### Watch the orchestrator run

    gh run list --repo my-org/my-agent-repo --workflow agentOS-orchestrator.yml

The orchestrator:

1. Reads the issue, determines the runner to dispatch (builder, reviewer, etc.)
   based on the current labels.
2. Transitions the issue to `status:in-progress` by removing `status:todo` and
   adding `status:in-progress`.
3. Dispatches the agentOS-builder workflow with the issue number as input.

The builder workflow then:

1. Checks out the repository.
2. Creates a branch named `agent/issue-<N>-<slug>`.
3. Invokes the configured runner (e.g., `hermes run --issue <N>`).
4. The runner loop builds code, commits, and opens a PR.
5. Applies `status:in-review` and `agent:reviewer` labels to the issue.

Follow live:

    gh run watch --repo my-org/my-agent-repo

When the builder completes successfully, you will find a new PR in your repository
with the branch `agent/issue-<N>-<slug>` and the issue labeled `status:in-review`.

---

## 10. Troubleshooting

### Missing secrets

Symptom: Workflow fails immediately with "Error: Context access might be invalid" or
"secret not found".

Cause: One or more GitHub Actions secrets were not uploaded, or were uploaded to the
wrong repository.

Fix:

    agentOS apply --repo my-org/my-agent-repo --only secrets

This re-uploads all App credentials without re-running the full apply. Verify with:

    gh secret list --repo my-org/my-agent-repo

You should see AGENTOS_BUILDER_APP_ID, AGENTOS_BUILDER_PRIVATE_KEY, and the
equivalent pairs for reviewer, watcher, and board.

### Wrong App permissions

Symptom: Workflow runs but fails with a 403 or "Resource not accessible by
integration" error partway through.

Cause: The GitHub App was created without the correct permissions, or the App
installation does not include the repository.

Fix:

1. Go to github.com/settings/apps (personal) or github.com/organizations/<org>/settings/apps.
2. Find the App (e.g., "my-agent-repo builder").
3. Click Permissions & events. Compare against the permission table in docs/agent-roles.md.
4. If permissions are missing, add them and click Save.
5. Go to Installations, find your org/personal account, click Configure, and ensure
   the target repository is in the list.

After fixing permissions, re-run the failed workflow from the Actions tab.

### board_id not set

Symptom: Board-related operations fail, or `agentOS verify` reports [FAIL] on the
Board check with "board_id is empty".

Cause: `agentOS apply` was interrupted before Step 2 completed, or the agentOS.yaml
change was not pushed to the default branch.

Fix:

    agentOS apply --repo my-org/my-agent-repo --only board

Then commit and push the updated agentOS.yaml:

    git add agentOS.yaml
    git commit -m "chore: update board_id"
    git push

### Orchestrator does not fire

Symptom: You applied `status:todo` to an issue but no workflow run appeared.

Causes and fixes:

- The workflow file is not on the default branch. Push any pending commits.
- The issue is missing a `type:*` label. The orchestrator filter requires both axes.
  Add `type:feature` (or another type label) and re-apply `status:todo`.
- GitHub Actions is disabled for the repository. Go to Settings -> Actions -> General
  and enable workflows.
- The workflow was disabled manually. Go to Actions, find agentOS-orchestrator, and
  click "Enable workflow".

### Runner exits non-zero immediately

Symptom: The builder workflow starts, the runner is invoked, but it exits with code 1
before doing any work.

Cause: The LLM provider API key secret is missing or invalid.

Fix: Add the secret to GitHub Actions:

    gh secret set ANTHROPIC_API_KEY --repo my-org/my-agent-repo

Then re-run the failed job from the Actions tab.

### Spec version mismatch

Symptom: `agentOS verify` reports a spec version warning.

Fix: Re-initialise with the pinned version you want:

    agentOS init --from github:open-agentos/spec@v1.4.0 --merge

The `--merge` flag updates agentOS.yaml in-place, preserving your local changes
(runner, plugins, board_id) while updating the spec-managed sections.

---

## Next Steps

- Read docs/label-model.md to understand the full label axis system.
- Read docs/agent-roles.md to understand what each App can and cannot do.
- Read docs/plugins.md to add domain-specific automation to your project.
- Read docs/metrics-schema.md if you want to analyse agent run data.
