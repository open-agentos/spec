# Uninstalling

This page is the manual reverse of
`agentOS apply` and `agentOS setup` — the same five things they provision,
undone in the same order, with the exact commands to run.

If you're trying this out for the first time and just want reassurance
nothing here can touch your source code: nothing `apply` does touches a
single line of code, a commit, or a branch. It creates labels, a project
board, two files in `.github/workflows/`, and some scaffold files. All of it
is listed below, and all of it can be deleted in about five minutes.

---

## What you're undoing

`agentOS apply` runs five steps. Each one is independently reversible:

| Step | What it created | Lives where |
|---|---|---|
| Labels | Up to 19 labels across 5 axes (`status`, `agent`, `type`, `review`, `source`) | GitHub repo labels |
| Board | A Projects v2 board, default name **"AgentOS Command Center"**, with 9 custom fields | GitHub Projects |
| Workflows | 4 workflow files | `.github/workflows/` in your repo |
| Scaffold | `AGENTS.md`, `config.yaml.example`, an `agents/` directory, `ops-metrics/.gitkeep` | repo root |
| Apps | GitHub App installations (only if you ran `agentOS setup`) | GitHub App settings |

Local-only files (`.env`, `*.pem`, `field-bindings.json`, `.agentOS-state.json`)
are already excluded from git by the `.gitignore` the spec seeds in — they
were never committed, so deleting them locally is enough; there's nothing to
scrub from history.

---

## 1. Remove the labels

agentOS never deletes labels on `apply` — by design, it won't touch labels
it didn't create or remove ones you added by hand. The same caution applies
in reverse: check what's actually present before bulk-deleting.

```bash
gh label list --repo owner/repo
```

The labels agentOS provisions follow this pattern (your `agentOS.yaml` may
have fewer if you trimmed it):

```
status:todo  status:in-progress  status:in-review  status:changes-requested
status:approved  status:blocked  status:planning  status:done
agent:builder  agent:reviewer  agent:docs  agent:watcher
type:feature  type:bug  type:chore  type:question
review:scope-violation
source:agent-created  source:human-created
follow-on:docs-needed
```

Delete them individually:

```bash
gh label delete "status:todo" --repo owner/repo --yes
```

Or script the whole set in one go — this only deletes labels matching the
agentOS axis prefixes, so anything you created yourself is untouched:

```bash
for label in $(gh label list --repo owner/repo --json name -q '.[].name' | grep -E '^(status|agent|type|review|source|follow-on):'); do
  gh label delete "$label" --repo owner/repo --yes
done
```

Any open issues that had these labels just lose the label — the issues
themselves are unaffected.

---

## 2. Remove the project board

The board is a GitHub Projects v2 board, separate from your repo and not
tied to git in any way. Deleting it doesn't touch any code or issues, only
the board's own view and fields.

1. Go to your org or user's **Projects** tab.
2. Open **"AgentOS Command Center"** (or whatever name your `agentOS.yaml`
   set under `board.name`).
3. Project settings (top right, `...` menu) → **Delete project**.

Or via the CLI:

```bash
gh project list --owner your-org
gh project delete <project-number> --owner your-org
```

If you'd rather keep the board but stop agentOS from touching it, leave it
in place and just skip steps 3–5 below — `apply` only re-provisions a board
if `field-bindings.json` (local-only, untracked) is missing or out of date.

---

## 3. Remove the workflow files

These are the only files agentOS writes that GitHub will actually *execute*.
Removing them stops every agent run immediately — no orchestrator, no
dispatch, nothing fires on label changes anymore.

```bash
cd your-repo
git rm .github/workflows/agent-orchestrator.yml
git rm .github/workflows/agent-settlement.yml
git rm .github/workflows/detect-run-failure.yml
git rm .github/workflows/run-receipt.yml
git commit -m "Remove agentOS workflows"
git push
```

If you customized any of these and `apply` skipped overwriting them (it
never force-overwrites a modified workflow), check the diff before deleting
in case you want to keep parts of it elsewhere.

---

## 4. Remove the scaffold files

These are static files with no automation tied to them — deleting them is
inert, same as deleting any other file in your repo.

```bash
git rm AGENTS.md
git rm config.yaml.example
git rm -r agents/
git rm -r ops-metrics/
git rm agentOS.yaml
git commit -m "Remove agentOS scaffold"
git push
```

Keep `agentOS.yaml` if you think you might reinstall later — it's just a
config file and does nothing sitting in the repo unread.

---

## 5. Revoke the GitHub Apps

Only relevant if you ran `agentOS setup`. Each role (`builder`, `reviewer`,
`watcher`, `board`) got its own GitHub App, named `agentOS-<role>` by
default (e.g. `agentOS-builder`).

**Uninstall from the repo** (keeps the App registration, just disconnects it
from this repo — do this if you might reuse the App elsewhere):

1. Repo **Settings** → **Integrations** → **GitHub Apps**.
2. Find each `agentOS-*` app → **Configure** → **Uninstall**.

**Delete the App entirely** (full removal, can't be undone — you'd register
a fresh one to reinstall):

1. `https://github.com/settings/apps` (personal) or
   `https://github.com/organizations/<org>/settings/apps` (org).
2. Open each `agentOS-*` app → **Advanced** → **Delete GitHub App**.

This also invalidates any private keys downloaded during setup — safe to
delete the local `.env` and any `.pem` files in `.agentOS/keys/` at this
point.

---

## 6. Clean up local files

None of these were ever committed, so this is just local housekeeping:

```bash
rm -f .env
rm -rf .agentOS/
rm -f field-bindings.json
rm -f .agentOS-state.json
```

---

## What's left after all six steps

Your repo is back to exactly what it was before `agentOS init`: no labels
beyond what you had, no board, no workflow files, no scaffold files, no App
installations. Open issues and PRs, their history, and your code are
untouched throughout — none of the five `apply` steps ever write to source
files, branches, or commits.

## Partial rollback

You don't have to do all six. Common partial cases:

- **Just want runs to stop, keep everything else for now** → step 3 only
  (delete the workflow files). Labels, board, and scaffold sit there
  inert with nothing left to act on them.
- **Want to keep the board/metrics but lose the labels** → step 1 only.
  The board won't auto-populate without the labels driving issue state,
  but historical data stays.
- **Tried `agentOS setup` but never ran `apply`** → just step 5. Nothing
  else was provisioned.