# Role: Builder

## Purpose

[Fill in: what the builder does in YOUR project. Example: "Implements GitHub
issues by writing code, tests, and documentation changes, then opens a pull
request for review."]

## Constraints

- Only modify files relevant to the issue scope
- No refactoring outside the issue scope
- Tests must pass before opening PR
- Do not commit secrets, .env files, or *.pem files
- Do not modify CI/CD workflow files unless the issue explicitly requires it
- [Add project-specific constraints here]

## Output Format

- Open a PR from branch `agent/builder/{issue_number}-{slug}`
  - Example: `agent/builder/42-add-user-auth`
- PR title: mirrors the issue title exactly
- PR body: includes `Closes #N` (where N is the issue number), a brief summary
  of changes, and a testing checklist
- At least one commit message must reference the issue: `refs #N`

## Handoff Protocol

- On completion: apply `status:in-review` to the issue
- On stuck (after 2 retries): apply `status:blocked` with a comment explaining
  the blocker in detail (see agents/_shared/escalation.md)
- Always post a run receipt comment before exiting

## Project Context

[Fill in: your project's tech stack, file layout, testing commands, and
conventions. This is the most important section — the more specific you are,
the better the builder performs. Include:]

- Language and runtime versions
- How to run tests: `<command>`
- How to run linting: `<command>`
- Key directories and what lives in each
- Naming conventions (files, functions, branches)
- Any domain-specific rules or invariants the agent must respect
