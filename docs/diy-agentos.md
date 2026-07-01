# Build Your Own Observable Agent Loop

### A DIY guide to label-driven multi-agent orchestration on GitHub

---

## Why this doc exists

If you want multiple AI agents to pick up work, act on it, and hand off to each other — all without disappearing into a black box — you don't need to install anything. GitHub already gives you a queue (Issues/PRs), a state machine (Labels), a scheduler (Actions), and an audit log (the event history) that you already trust with your source code. This guide shows you how to wire those primitives together yourself.

Two things this system needs to actually deliver:

1. **The loop** — agents pick up work, do it, and hand off, without a human babysitting every step.
2. **The receipts** — a human can see exactly what happened, who (which agent) did it, and why, after the fact.

Most write-ups only cover #1. #2 is what makes #1 safe to trust, so it gets equal billing here. Some specifics below (label names, folder layout) are deliberately generic — swap them for whatever fits your repo. The *steps* are the part that matters.

---

## Repo Layout

Here's where everything below ends up. Worth seeing the destination before the steps:

```
.
├── README.md                    # points here first — links to docs/LABELS.md and docs/IDENTITIES.md
│
├── .github/
│   └── workflows/
│       ├── planner-agent.yml    # 1.3 — intake, turns status:new into status:queued
│       ├── builder-agent.yml    # 1.3 — dispatch workflow, one per role
│       ├── reviewer-agent.yml
│       ├── docs-agent.yml
│       └── weekly-digest.yml    # 2.3 — only if you pick the digest surface
│
├── AGENTS/
│   ├── planner.md                # 1.4 — role brief, includes decomposition granularity
│   ├── builder.md                # 1.4 — role brief: ownership, DoD, constraints, escalation
│   ├── reviewer.md
│   └── docs.md
│
├── scripts/
│   ├── run_agent.py              # 1.3 — pulls context, calls the LLM, writes back
│   ├── check_permission.py       # 1.5 — live permission gate, called before consequential actions
│   └── write_trace.py            # 2.2 — append-safe JSONL writer, called at the end of every run
│
├── docs/
│   ├── LABELS.md                 # 1.1 — taxonomy source of truth: every axis, every value
│   └── IDENTITIES.md             # 1.2 / 2.4 — role → identity → scope mapping
│
└── trace/
    └── agent-trace.jsonl         # 2.2 — the append-only log itself, committed and diffable
```

A few choices worth flagging:

- **`trace/` sits at the top level, not hidden.** Burying the audit trail in a dotfolder undercuts the whole point of Part 2 — it should be as easy to find as `src/`.
- **`LABELS.md` and `IDENTITIES.md` are separate files.** They answer different questions — what state can this be in, vs. who's allowed to act on it — and tend to get reviewed by different people, so keeping them apart makes both PR-reviewable on their own.
- **Three small scripts, not one monolith.** `check_permission.py` and `write_trace.py` are pulled out of `run_agent.py` on purpose — the gate and the trace are the two things a skeptical reader will want to audit independently of "does the agent's logic work."
- **No secrets anywhere in the tree.** Credentials from 1.2 live in GitHub's encrypted secrets store, never as files.
- **No dashboard directory by default.** If you pick that surface in 2.3 over the digest or board view, add a `dashboard/` reading `trace/agent-trace.jsonl` — left out here since 2.3 says pick one, and board/digest need no extra files at all.

**Prompt for a coding agent:**
```
I'm setting up a label-driven multi-agent orchestration system in my GitHub
repo [OWNER/REPO]. Scaffold this exact structure, creating empty
files/folders where content will be filled in later — don't add any logic
yet, I'll come back section by section with specific prompts for each piece:

.github/workflows/
AGENTS/
scripts/
docs/LABELS.md
docs/IDENTITIES.md
trace/agent-trace.jsonl
README.md linking to docs/LABELS.md and docs/IDENTITIES.md

Confirm the skeleton is in place and committed, then stop.
```

---

## Part 1: The Core Loop

### 1.1 Design your label taxonomy (this is your state machine)

Don't reach for a database or a workflow engine. A small set of labels, organized into axes, *is* your state machine. Each axis answers one question:

| Axis | Question it answers | Example values |
|---|---|---|
| `status:*` | Where is this in the pipeline? | `new`, `queued`, `in-progress`, `blocked`, `needs-review`, `done`, `failed` |
| `agent:*` | Which agent owns it right now? | `planner`, `builder`, `reviewer`, `docs` |
| `type:*` | What kind of work is it? | `bug`, `feature`, `docs` |
| `review:*` | What did the review gate decide? | `needs-review`, `approved`, `changes-requested` |

An issue's current labels tell you its exact position in the pipeline — no separate tracking system needed. A **state transition is just a label change**, which means every transition is already timestamped and logged by GitHub for free.

`status:new` is where every raw request starts, before anything else touches it. The Planner is the only thing allowed to move an issue out of `status:new` — it either relabels the issue directly with `agent:*`/`type:*`/`status:queued`, or splits it into child issues that each carry those labels. Nothing else should ever hand-apply `status:queued`; if you catch yourself doing that, the Planner step got skipped.

Two things worth getting right early, because they're annoying to fix later:
- Pick label names that won't collide with anything GitHub reserves internally (e.g. a Projects v2 field literally named `Status` will fail — `Agent Status` won't).
- Decide your taxonomy before you write any workflow files. Adding an axis later means touching every workflow that reads labels.

**Do this:**
1. List every axis you actually need (status, agent, type, review — add more only if a real question demands it).
2. For each axis, write the exhaustive list of values *before* creating any labels.
3. Check every proposed name against platform-reserved words (e.g. Projects v2 field names).
4. Create all labels in one batch via `gh label create` or the API — don't hand-create them one at a time as you go.
5. Commit the taxonomy itself as a file in the repo (e.g. `LABELS.md`) so it's the source of truth, not tribal knowledge.

**Prompt for a coding agent:**
```
Help me set up the label taxonomy for a label-driven agent orchestration
system in [OWNER/REPO].

Axes and values:
- status: new, queued, in-progress, blocked, needs-review, done, failed
- agent: planner, builder, reviewer, docs
- type: bug, feature, docs
- review: needs-review, approved, changes-requested

1. Check these names against GitHub's reserved words (e.g. Projects v2
   field names) and flag any conflicts before creating anything.
2. Write the `gh label create` commands to create every label in one
   batch, with sensible colors grouped by axis.
3. Write docs/LABELS.md documenting each axis, its values, and what each
   value means — including that status:new is the only entry point, and
   only the Planner may move an issue out of it.

Show me the commands before running anything that creates labels in the
live repo.
```

### 1.2 Give each agent its own identity

This is the step people skip, and it's the one that makes everything in Part 2 meaningful. Don't run every agent under one shared token.

Register one identity per role — either a dedicated GitHub App per role, or at minimum a separate machine account with a fine-grained PAT. Scope each one to the minimum it needs:

- **Planner**: `issues: write` only — no `contents` access at all. It can read, create, and label issues, but never touches code.
- **Builder**: `contents: write`, `issues: write`, `pull_requests: write`
- **Reviewer**: `pull_requests: write` only — no `contents: write`, so it physically cannot push code, only comment/approve
- **Docs**: `contents: write` scoped to a `docs/` path if your token type supports path restrictions

Notice Planner has the narrowest scope of any role, despite going first — deciding what work exists isn't the same as being trusted to do it.

Store each identity's credentials as separate secrets (e.g. `BUILDER_APP_ID` / `BUILDER_PRIVATE_KEY`, `REVIEWER_APP_ID` / `REVIEWER_PRIVATE_KEY`). Note: GitHub rejects secret names starting with `GITHUB_`, so plan your naming convention before you have five of these to rename.

**Do this:**
1. List every role in your loop before registering anything.
2. For each role, decide GitHub App vs. machine account + fine-grained PAT.
3. Register each identity with only the scopes that role needs — nothing broader "to be safe."
4. Generate and download credentials for each identity separately.
5. Store each as its own secret using a consistent naming pattern (e.g. `{ROLE}_APP_ID`, `{ROLE}_PRIVATE_KEY`), and confirm none start with the reserved `GITHUB_` prefix.
6. Write down the role → identity → scope mapping somewhere durable — not just in your head.

**Prompt for a coding agent:**
```
Walk me through registering GitHub Apps for the agent identities in a
label-driven orchestration system on [OWNER/REPO].

Roles and scopes needed:
- Planner: issues:write only — no contents access at all
- Builder: contents:write, issues:write, pull_requests:write
- Reviewer: pull_requests:write only
- Docs: contents:write (scoped to docs/ if possible)

Give me exact, numbered, click-by-click steps for:
1. Registering one GitHub App per role in [ORG]'s org settings (not my
   personal account) with only the scopes above.
2. Generating and downloading each App's private key.
3. Installing each App on this specific repo.
4. Adding each App's ID and private key as repo secrets, using the naming
   pattern {ROLE}_APP_ID / {ROLE}_PRIVATE_KEY — including exactly where
   in GitHub's Settings UI to add them.

Flag anything in this process that needs org-owner permissions I might
not have, before I get stuck partway through. This touches real
credentials, so walk me through it rather than trying to do it for me.
```

### 1.3 Wire the dispatch (label → agent)

A GitHub Actions workflow per role, triggered when its label is applied:

```yaml
name: builder-agent
on:
  issues:
    types: [labeled]

jobs:
  run-builder:
    if: github.event.label.name == 'agent:builder'
    runs-on: ubuntu-latest
    permissions:
      contents: write
      issues: write
    steps:
      - uses: actions/checkout@v4

      - name: Run builder agent
        env:
          LLM_API_KEY: ${{ secrets.BUILDER_LLM_KEY }}
        run: |
          python scripts/run_agent.py \
            --role builder \
            --issue "${{ github.event.issue.number }}" \
            --brief AGENTS/builder.md

      - name: Hand off to review
        run: |
          gh issue edit ${{ github.event.issue.number }} \
            --remove-label "status:in-progress" \
            --add-label "status:needs-review"
```

`run_agent.py` is the only truly custom part: it pulls the issue body/comments as context, calls your LLM of choice with the role's brief (below), and writes the result back as a commit, PR, or comment. Everything around it is off-the-shelf GitHub Actions.

The Planner's workflow has the same shape — same trigger pattern, same `run_agent.py` call — but it triggers on `status:new` instead of an `agent:*` label, and its job is different: read the raw request and either relabel it directly (`agent:*`/`type:*`/`status:queued`) or create one or more child issues carrying those labels, closing the original as a tracking parent. Everything downstream never sees the difference — it only ever picks up properly labeled `status:queued` issues.

**Do this:**
1. Create one workflow file per role — resist combining roles into one file.
2. Set the trigger event and the label-name filter (`if: github.event.label.name == '...'`).
3. Set the job's `permissions:` block to match that role's identity exactly — no wider than 1.2's scopes.
4. Point the run step at that role's runner script and its brief file.
5. Add the label-flip step as the last step in the job itself, not as a separate manual action.
6. Test by manually applying the label to a throwaway issue before wiring it into the full loop.

**Prompt for a coding agent:**
```
Write the GitHub Actions dispatch workflows for a label-driven agent loop
in this repo, based on:

- Labels: agent:planner, agent:builder, agent:reviewer, agent:docs (see
  docs/LABELS.md)
- One workflow per role in .github/workflows/, triggered on
  issues.labeled (Planner's workflow triggers on status:new instead)
- Each workflow's `permissions:` block should match exactly what's
  registered for that role's identity in docs/IDENTITIES.md — don't
  grant more
- Each workflow calls scripts/run_agent.py with --role, --issue, and
  --brief pointing at the matching AGENTS/<role>.md file
- Last step: Planner either relabels the issue directly or creates child
  issues carrying agent:*/type:*/status:queued; every other role flips
  the status label to the next state

Also write the "gate" workflow: triggered when review:approved is
applied, or when the Planner is about to release a plan into
status:queued, calls scripts/check_permission.py first, and only
proceeds if that check passes.

Show me the files before committing them.
```

### 1.4 Give each agent a brief, not a buried prompt

Put each role's instructions in a versioned file in the repo — not in a system prompt no one but you can see:

```markdown
# Role: Builder Agent

## What you own
Implement issues labeled `agent:builder` once they carry `status:queued`.

## Definition of done
- Tests pass locally
- A PR is opened referencing the issue
- Label flipped to `status:needs-review`

## Constraints
- Never touch files under `infra/`
- Never merge your own PR

## Escalation
If the task is ambiguous, comment `@human-review` and add `status:blocked`.
Do not guess.
```

This does double duty: it's the agent's context *and* a human-readable spec of what each agent is allowed to do, sitting in your repo where anyone can read it without asking you.

Planner's brief needs one thing the others don't: an explicit target granularity for decomposition. Without it, Planner will either dump one giant issue on Builder — defeating the point — or fragment trivial work into a dozen issues nobody wants to review. State it directly, e.g. "each child issue should be completable in one PR," rather than leaving it to judgment.

**Do this:**
1. Create one folder for briefs (e.g. `AGENTS/`).
2. Write one file per role: ownership, definition of done, constraints, escalation rule.
3. Reference the brief's file path in the workflow — don't paste its contents inline.
4. Route changes to a brief through a normal PR review, same as code.
5. Write the escalation instruction explicitly — agents need a stated "stop and ask" condition, they won't infer one.
6. For Planner specifically, state the target decomposition granularity in the brief — don't leave "how big should a task be" to inference.

**Prompt for a coding agent:**
```
Draft AGENTS/planner.md, AGENTS/builder.md, AGENTS/reviewer.md, and
AGENTS/docs.md for my agent loop, each following this structure:

- What you own (which labels/state trigger this role)
- Definition of done
- Constraints (what this role must never do)
- Escalation (the exact condition for stopping and asking a human, and
  how — e.g. commenting @human-review and adding status:blocked)

For Planner specifically: it turns status:new issues into properly
labeled status:queued issues (directly, or via child issues). Give it an
explicit target granularity for decomposition — e.g. "each child issue
should be completable in one PR" — and make clear it never writes code
itself.

Here's what I want each role to actually do: [describe planner/builder/
reviewer/docs responsibilities in your own words, or point me at
existing code or docs you want them to follow].

Keep each file under a page. Ask me clarifying questions about scope
before you write anything you're unsure of, rather than guessing.
```

### 1.5 The approval gate — check permissions live, not just labels

A label alone can be gamed by stale state: someone's access gets revoked, but a `review:approved` label from last week is still sitting there. Before anything consequential happens (a merge, a deploy trigger), check the actor's *current* permission level via the API — not just whether the right label exists:

```bash
gh api repos/$OWNER/$REPO/collaborators/$ACTOR/permission \
  --jq '.permission'
# → admin | write | read | none
```

Only proceed if the live check passes. This turns "does this look approved" into "is this actually authorized, right now."

The same logic applies one step earlier. A Planner's proposed decomposition is consequential too — it's what determines which work exists and which agents get triggered at all. Gate the moment a plan actually flips its issues to `status:queued` behind the same kind of live check you'd use for a merge. That's what keeps "deciding what work exists" from becoming a silent, ungated power on its own.

**Do this:**
1. List every action in your loop that's consequential (merge, deploy, delete, releasing a Planner's plan into `status:queued` — anything hard to undo or that triggers further automated action).
2. For each one, add a live permission check immediately before it executes — not back at trigger time.
3. Define the minimum permission level required for that specific action.
4. Fail closed: if the check errors or returns insufficient permission, block the action and apply `status:blocked` — don't proceed by default.
5. Log the check's result (pass/fail, actor, level) to your trace file (see 2.2), so the gate itself is auditable.

**Prompt for a coding agent:**
```
Write scripts/check_permission.py for the approval gate in my agent loop.

Requirements:
- Takes a GitHub actor and repo as input, calls the GitHub API to check
  their CURRENT permission level — not a cached or label-based one
- Returns pass/fail against a minimum required level I specify per call
  site (e.g. "write" for merges, "write" for releasing a Planner's
  proposed plan)
- On any error or insufficient permission, fails closed — never defaults
  to allowing the action
- Writes its result (pass/fail, actor, level checked, timestamp) as a
  line to trace/agent-trace.jsonl, using the same format as
  scripts/write_trace.py

Wire it into two places: the merge/gate workflow from 1.3 immediately
before the merge step, and the Planner's workflow immediately before it
flips any issue to status:queued.

Show me the diff before applying it.
```

That's the full loop: **new request → Planner decomposes it into scoped tasks, gated by a live permission check → queued → agent picks it up via label → agent acts under its own identity → hands off via label → live permission check gates the risky step → done.**

---

## Part 2: Observability & Accountability (the payoff)

This is the part that makes the loop above trustworthy enough to actually run unattended. The good news: if you did Part 1 correctly, you get most of it for free.

### 2.1 You already have an audit trail

If every agent action is a real GitHub event — a commit, a PR comment, a label change — under a *distinct bot identity* (1.2), you already have a full record: who did what, when, and why (the commit message or comment body), with zero extra logging infrastructure. This is the structural difference between this approach and most agent frameworks, where work happens inside a process and you only ever see the final diff, not the decisions along the way.

**Do this:**
1. Walk your workflow files and confirm every agent action produces a real GitHub event — nothing should happen silently inside a script with no trace written back to the repo.
2. Confirm each event is authored under the correct role's identity, not a generic bot or your own account.
3. Pick three recent runs at random and try to reconstruct exactly what happened using only the GitHub UI. If you can't, something's still happening off the record — find it and surface it.

**Prompt for a coding agent:**
```
Audit this repo's agent workflows in .github/workflows/ and confirm every
agent action produces a visible GitHub event — a commit, PR, comment, or
label change — under the correct role's bot identity, with nothing
happening silently inside a script.

Then pick 3 recent closed issues that went through the agent loop and
reconstruct, using only GitHub's UI/API (not the trace file), exactly
what happened to each one — who acted, when, and what changed.

Report back anything you can't reconstruct that way — that's a gap in
the audit trail, not just a documentation gap.
```

### 2.2 Make it queryable

The native GitHub event history is complete but not convenient to query. Add one small thing: have each agent append a line to an append-only trace file after every action.

```json
{"ts":"2026-07-01T09:14:02Z","role":"builder","issue":42,"action":"opened_pr","pr":57,"model":"claude-sonnet-5","cost_usd":0.18,"outcome":"success"}
```

JSONL is the right format for this, not a database: it's append-only, diffs cleanly in git, and needs no schema migration when you add a field. Now "what did the reviewer agent do this week" is a `grep`/`jq` away instead of a manual scroll through Issues.

**Do this:**
1. Define your trace schema up front — timestamp, role, issue/PR, action, model, cost, outcome — and add fields deliberately, not as you go.
2. Add a "write trace line" step as the final step of every agent workflow.
3. Decide where it lives: a committed JSONL file (simplest, git-diffable) or an external store if volume demands it.
4. Make the write append-safe so two near-simultaneous runs can't corrupt the file.
5. Save one or two `jq` query recipes in your docs so the trace gets used, not just accumulated.

**Prompt for a coding agent:**
```
Write scripts/write_trace.py for my agent loop.

Schema for each line in trace/agent-trace.jsonl: ts, role, issue (or pr),
action, model, cost_usd, outcome. Make it append-only and safe against
two workflows writing at the same moment (file locking or an atomic
append).

Wire it as the last step in every workflow in .github/workflows/, called
after the agent's main action.

Also give me two or three jq one-liners I can save in docs/ — e.g. total
cost this week, and all actions by one role — so the trace actually gets
used.
```

### 2.3 Surface it to humans who won't read raw JSONL

Pick whichever of these matches your audience — this is the part that's genuinely fine to customize:

- A **Projects v2 board view**, sliced by your `status:*`/`agent:*` labels — free, native, zero extra tooling
- A **small dashboard** reading the JSONL file — a chart of runs, cost, and outcomes per agent
- A **scheduled digest**, posted as a comment on a tracking issue: *"This week: 14 runs, 2 blocked, $6.10 spent, 0 unauthorized-merge attempts."*

The point isn't the specific tool — it's that a non-technical stakeholder can glance at *something* and know what the agents did, without reading YAML.

**Do this:**
1. Pick exactly one surface for v1 — board view, dashboard, or digest. Don't build all three.
2. If board view: slice a Projects v2 view by your `status:*`/`agent:*` labels.
3. If dashboard: build the smallest possible read-only view over the JSONL file — one table and one chart is enough to start.
4. If digest: add a scheduled workflow that summarizes the week's trace lines into a single posted comment.
5. Hand it to the actual non-technical stakeholder and watch them use it once, unassisted, before calling it done.

**Prompt for a coding agent:**
```
Help me build one way to surface the agent trace to a non-technical
stakeholder. I want: [board view / small dashboard / weekly digest
comment — pick one].

- If board view: configure a GitHub Projects v2 view sliced by
  status:*/agent:* labels — walk me through the setup since this is
  mostly UI, not code.
- If dashboard: build the smallest possible read-only page reading
  trace/agent-trace.jsonl — one table, one chart.
- If digest: write a scheduled workflow that summarizes the week's trace
  lines into a posted comment on a tracking issue.

Don't build the other two options — just the one I picked.
```

### 2.4 Why the identity boundary is what makes any of this real

Tie this back to 1.2: the trace and the dashboard are only as trustworthy as the identity boundary underneath them. If every agent runs under one shared token, your "audit trail" can tell you *that* something happened but not *which* agent's logic, under *which* permission scope, decided to do it. Per-role identity is what lets a human look at the repo and say "the builder agent did this, it only ever had `contents:write`, and I can revoke just that one role without touching the others." That sentence is the actual accountability payoff — everything else is UI on top of it.

Same logic covers Planner, and it's arguably the more important case: if its decomposition is wrong, the blast radius is a bad task list someone has to clean up, not code that shipped — because it never had `contents` access to begin with.

**Do this:**
1. Audit each role's live permission grant and confirm it still matches what you intended in 1.2 — permissions drift as repos evolve.
2. Pick one role and actually test revoking its access in a non-production repo; confirm the others keep working untouched.
3. Document the role → identity → permission mapping next to the trace output, so a reader can verify the accountability claim rather than take it on faith.

**Prompt for a coding agent:**
```
Audit the identity boundary in this repo: for each role (planner,
builder, reviewer, docs), confirm its GitHub App or PAT actually holds
only the scopes listed in docs/IDENTITIES.md — no more.

Then help me test isolation: in a non-production repo, walk me through
revoking one role's access and confirming the other roles' workflows
still run unaffected.

Update docs/IDENTITIES.md with the confirmed role → identity → scope
mapping so it reflects reality, not just intent.
```

---

## What you're signing up to maintain

None of the above is hard in isolation. It gets tedious in combination, and this is the part worth knowing before you commit:

- **Idempotency.** GitHub redelivers webhooks. A workflow that assumes it's the only listener for a label event will eventually double-run. Check current state before acting, don't assume you're first.
- **Secret sprawl.** One identity per role means N credentials to rotate instead of one, and naming collisions (that `GITHUB_` prefix rejection) bite the first time you're not expecting them.
- **Race conditions.** Two near-simultaneous label changes on the same issue can trigger conflicting workflow runs. Decide your conflict resolution (last-write-wins, a lock label, whatever) explicitly — don't discover it in production.
- **Reserved-name collisions.** Field and label names can silently collide with platform-reserved words. Verify your taxonomy against the platform before you build workflows around it.
- **Drift.** Every time you add an agent or a label, your workflow files, taxonomy, and `AGENT.md` briefs all need to move together. Nothing enforces that for you.
- **Planner scope creep.** Nothing stops a Planner from quietly turning a two-line request into fifteen child issues. Cap it explicitly (a max child-issue count, or a required human nod above some size) rather than trusting judgment alone — review its decomposition output the way you'd review its code.

None of this is a reason not to build it yourself — it's just the honest maintenance cost, and it's worth knowing upfront rather than discovering it a workflow at a time.

---

## What's next

The prompts above are a first cut at making this activatable, not a tested one. The real next step is running them end-to-end against a real experimental repo — noting anywhere a prompt assumed context it didn't actually have, or where a coding agent's output needed correcting — and folding those fixes back into the prompts themselves.