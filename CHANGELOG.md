# Changelog

All notable changes to the GitHub AgentOS Spec are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/).

Breaking changes (label renames, field removals, workflow interface changes) bump the major
version. Each major bump ships a corresponding MIGRATION-vN.md in the repo root.

---

## [1.1.0] — 2026-06-29

Planning stage and dispatch-time approval gate. No breaking changes — all existing
labels and workflows continue to work. The new planning stage is additive; operators
on `governance.planning: off` see no behaviour change.

`specVersion` in `agentOS.yaml` bumps from `"1.0"` to `"1.1"` to reflect the new
governance block, planner role, and label axis additions described below. This is
the field `agentOS upgrade` reads to detect drift, and the value embedded in
generated `agent-orchestrator.yml` workflows via `{{SPEC_VERSION}}`.

### Fixed

- The PyPI package is published as `open-agentos-cli`. The bare name
  `open-agentos` (and the docs' prior `agentOS-cli`, which never matched
  either) collided with PyPI's name-similarity check against an existing,
  unrelated `agentos` package. `pyproject.toml` and every `pip install` /
  `uv tool install` instruction in the docs now consistently reference
  `open-agentos-cli`. Note this only affects the PyPI package name — the
  GitHub repo (`open-agentos/agentos`), the `agentOS` CLI command, and the
  `bootstrap` Python module are unchanged.

- `bootstrap/agentOS.yaml` and `bootstrap/templates/` (the copies bundled into the
  installable package and used by `agentOS init` with no `--from` source) had
  drifted from the canonical root `agentOS.yaml` / `templates/` and were missing
  the planner role, governance block, and related labels added below. Re-synced;
  `agentOS init` with no source and `agentOS init --from github:open-agentos/agentos`
  now produce identical output.

### Added

**Labels (3 new)**
- `status:plan` — planner entry point; dispatches planner role on label event
- `status:plan-review` — plan written into body; awaiting admin `/approve-plan`
- `agent:planner` — ownership label set when planner is running

**agentOS.yaml**
- `governance:` config block with `planning`, `approvers`, `approve_command`,
  `changes_command`, `plan_begin_marker`, `plan_end_marker` fields
- `governance.planning: required` default (zero-config happy path)
- Planner role definition (uncommented, core, enabled by default);
  reuses builder App as interim token source with a TODO to split
- `status:plan` / `status:plan-review` entries in the status axis with `routes_to`
- `agent:planner` entry in the agent axis
- Board field options for Plan / Plan review status values and Planner role value

**Orchestrator template**
- Added `issue_comment: [created]` trigger to `templates/workflows/agent-orchestrator.yml`
- `plan-orchestrator` job with `concurrency: planner-{issue_number}` group
- Dispatch-time approval gate (github-script step):
  - `/approve-plan` command: verifies commenter permission live via GitHub API,
    checks plan block present, checks approval postdates latest plan receipt;
    dispatches builder only when all conditions pass
  - `/request-changes` command: verifies permission, resets to `status:plan`
  - `status:todo` label guard: silently skips builder dispatch when no valid approval
- `sync-close-label` updated to remove `status:plan` and `status:plan-review` on close

**Templates**
- `templates/agents/planner/AGENT.md.template` — full planner role spec:
  in-body plan writing, marker contract, idempotency rule, status transitions,
  receipt format, run-record emission
- `templates/agents/builder/AGENT.md.template` — "Inputs — Plan Consumption" section:
  plan block detection, authoritative contract rule, fallback to full body

**SPEC.md**
- Section 3.2: updated state machine diagram with `status:plan` and `status:plan-review`
- Section 3.3: updated routing table
- Section 3.6: new normative section — Planning Stage and Dispatch-time Approval:
  two planning states (§3.6.1), marker contract (§3.6.2), approval semantics (§3.6.3),
  slash commands (§3.6.4), governance config (§3.6.5), idempotency/concurrency (§3.6.6)
- Section 4.2: updated planner description
- Section 11: updated conformance requirements
- Appendix A: added new label colours (`status:plan`, `status:plan-review`, `agent:planner`)
- Appendix C: Plan body template (CE-style, with AGENTOS:PLAN:BEGIN/END markers)
- Appendix D: Approval gate design notes (why live permission, not stored label)

**docs/label-model.md**
- Updated status label table with `status:plan` and `status:plan-review`
- New state machine diagram with full planning path
- Updated routes_to table
- New "What happens on each transition" entries for `status:plan` and `status:plan-review`
- Updated `agent:planner` description

**docs/agent-roles.md**
- Section 7 (planner): complete rewrite — in-body planning, marker contract,
  permission model, concurrency, manual entry points, receipt format
- Section 7.1 (new): Dispatch-time approval gate — how approval is verified,
  why live permission, `/approve-plan` and `/request-changes` semantics,
  governance config reference

---

## [1.0.0-alpha] — 2026-06-26

First public release of the GitHub AgentOS Spec. Extracted from the 3qs-ops
reference implementation (https://github.com/mattmcalister/3qs-ops).

### Added

**Specification**
- `agentOS.yaml` — importable core spec: 4 agent roles, 6 label axes (25 labels),
  10 Projects v2 board fields, JSONL metrics schema v6, plugin interface
- `SPEC.md` — full normative document (11 sections, 2 appendices)
- `schema/agentOS.schema.json` — JSON Schema for agentOS.yaml validation
- `schema/run-record.schema.json` — JSON Schema for JSONL v6 run + settlement records
- `schema/field-bindings.schema.json` — JSON Schema for live GraphQL node ID mapping
- `schema/project-rules.schema.json` — JSON Schema for project-rules.json

**Bootstrap CLI** (`pip install open-agentos-cli`)
- `agentOS init` — generate agentOS.yaml from spec or blank template
- `agentOS setup` — interactive GitHub App manifest wizard (4 apps)
- `agentOS apply` — idempotent provisioner: labels → board → workflows → scaffold
- `agentOS verify` — health check against agentOS.yaml
- `agentOS token` — mint a short-lived App installation token
- State tracking via `.agentOS-state.json` (resume from failure)
- `--dry-run`, `--only`, `--skip`, `--reset` flags

**Templates**
- `templates/workflows/` — 4 parameterised GHA workflows (orchestrator, settlement,
  failure detection, run receipt); template vars expanded by `agentOS apply`
- `templates/AGENTS.md` — runtime-agnostic operating manual template
- `templates/agents/{role}/AGENT.md.template` — fill-in-the-blank role stubs
- `templates/agents/_shared/` — context management, escalation, loop, telemetry guides
- `templates/config.yaml.example` — generalised runtime config template

**Scripts**
- `scripts/run_record.py` — RunRecord dataclass, JSONL schema v6
- `scripts/publish_record.py` — JSONL corpus writer with retry/rebase
- `scripts/metrics.py` — cost aggregator and corpus analytics
- `scripts/cost_rates.py` — model rate table loader
- `scripts/model_rates.yml` — reference rates for 5 models (with staleness warning)
- `scripts/projector.py` — Projects v2 field updater; board_id from env/bindings
- `scripts/promote_ready.py` — optional PR promotion scanner

**Plugin System**
- `plugins/README.md` — plugin authoring guide
- `plugins/three-questions/` — reference plugin: phase labels, watcher schedule,
  follow-on:dreaming-needed, watcher AGENT.md template

**Documentation**
- `docs/getting-started.md` — 30-minute end-to-end walkthrough
- `docs/label-model.md` — full label axis reference with routing table
- `docs/agent-roles.md` — permission model, runtime interface, runner examples
- `docs/metrics-schema.md` — JSONL v6 field-by-field reference
- `docs/plugins.md` — plugin development guide

**Tests**
- 20 tests covering labels, state machine, apply orchestration, board fingerprinting
- `tests/fixtures/` — sample agentOS.yaml, sample JSONL run records
