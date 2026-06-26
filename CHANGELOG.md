# Changelog

All notable changes to the GitHub AgentOS Spec are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/).

Breaking changes (label renames, field removals, workflow interface changes) bump the major
version. Each major bump ships a corresponding MIGRATION-vN.md in the repo root.

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

**Bootstrap CLI** (`pip install agentOS-cli`)
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
