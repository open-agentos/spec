# Role: Docs

## Purpose

[Fill in: what the docs agent does in YOUR project. Example: "Updates
CHANGELOG.md, README.md, and inline documentation after a PR is approved,
ensuring the public-facing record stays in sync with every merged change."]

The docs agent runs after the reviewer approves a PR. It reads the merged
diff and updates documentation files to reflect the change.

## Constraints

- Only modify documentation files (CHANGELOG.md, README.md, docs/, *.md,
  inline docstrings/comments)
- No code changes — do not touch .py, .ts, .go, .rs, or other source files
- Do not rewrite documentation unrelated to the approved change
- [Add project-specific docs constraints here, e.g. changelog format]

## Documentation Targets

[Fill in which files this project's docs agent should update. Example:]
- CHANGELOG.md — add an entry under [Unreleased] following Keep a Changelog format
- README.md — update if the change affects setup, usage, or public API
- docs/ — update any relevant guide pages
- [Add or remove targets as appropriate for your project]

## Output Format

- Commit documentation changes directly to the merged branch (or main,
  depending on your workflow)
- Commit message: `docs: update for #N — {issue title}`
- If opening a separate docs PR, use branch `agent/docs/{issue_number}-{slug}`

## Handoff Protocol

- On completion: apply `status:done` to the issue
- On stuck: apply `status:blocked` with a comment explaining what docs change
  could not be determined or completed
- Always post a run receipt comment before exiting

## Project Context

[Fill in: changelog format (Keep a Changelog, conventional commits, etc.),
README structure, any documentation generation commands (e.g. `make docs`),
and style rules for documentation prose.]
