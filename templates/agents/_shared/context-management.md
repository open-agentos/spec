# Context Management

Agents run inside a finite context window. Every token you read costs context
budget. Manage it deliberately.

## Core Principle: Read Only What You Need

Before opening any file, ask: do I actually need this to complete the task?

- Read the issue body and title first. Understand the scope.
- Read only the files the issue mentions or that are directly relevant.
- If a file is large, read the specific section (use line ranges if your runner
  supports it) rather than the whole file.
- Avoid reading test fixtures, lock files, or generated code unless the issue
  is specifically about them.

## Break Large Tasks into Focused Sessions

A single agent session should accomplish one coherent unit of work. If an issue
spans multiple subsystems or requires changes to many unrelated files, consider:

- Asking a human to split the issue before starting.
- Completing the highest-priority sub-task and opening a PR for it, then
  creating a follow-on issue for the remainder.
- Applying status:blocked if the scope is genuinely unclear.

Do not try to solve everything in one session if it means thrashing through
the entire codebase.

## Compression

If your runner provides a context compression command, invoke it after completing
a major phase of work (e.g., after reading and analysing the issue, before
starting to write code). This frees up space for the implementation phase.

Refer to your runner's documentation for the correct compression command.
Not all runners support mid-session compression; if yours does not, budget
context more conservatively at the start.

## Context Budget Guidelines

- Reserve at least 20% of your context window for the output phase (writing
  code, composing comments, calling tools).
- If you find yourself near the limit before finishing, stop, post a partial
  progress comment on the issue, and apply status:blocked rather than
  producing incomplete or truncated output.
- Always leave enough context to write the run receipt comment on exit.

## Signs You Are Burning Context Unnecessarily

- Reading the same file multiple times in one session
- Loading entire directories to find one symbol
- Re-reading the issue body after every tool call
- Loading dependency lock files or auto-generated files
