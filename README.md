# GitHub AgentOS Spec

**A portable, versioned specification for label-driven multi-agent orchestration on GitHub.**

AgentOS turns a GitHub repository into a self-operating development system. Issues move through a
status-label state machine. Each transition fires a GitHub Actions workflow that routes work to the
right AI agent. Agents act, post receipts, and hand off — all through standard GitHub primitives.

You import this spec into your repo and a bootstrap CLI provisions everything: labels, a Projects v2
command-center board, GitHub App identities, and workflow files. From zero to a working agent loop
in under 30 minutes.

---

## Quickstart

```sh
# 1. Install the CLI
pip install agentOS-cli

# 2. Initialise a spec file in your project
agentOS init --from github:open-agentos/github-agentOS-spec@v1.0

# 3. Register GitHub Apps (interactive wizard — opens browser once per role)
agentOS setup --repo owner/my-repo

# 4. Provision labels, board, and workflows
agentOS apply --repo owner/my-repo

# 5. Verify everything is wired up
agentOS verify --repo owner/my-repo
```

## How it works

```
Issue labeled status:todo
        |
        v
agent-orchestrator.yml fires
        |
        v
Dispatches: AGENT_ROLE=builder ISSUE_NUMBER=42 GITHUB_TOKEN=...
        |
        v
Your runner command executes (hermes / claude / codex / custom)
        |
        v
Agent opens PR, labels issue status:in-review
        |
        v
Reviewer agent fires, approves or requests changes
        |
        v
PR merged -> settlement workflow finalises board
```

The spec defines the protocol (labels, routing, board fields, metrics schema). Your agent runtime
is a configuration value — bring Hermes, Claude Code, Codex, or your own script.

## What is provisioned

- **25 labels** across 5 axes (status, agent, type, review, source)
- **Projects v2 board** with 10 fields (metadata + telemetry, including 5 default model options)
- **4 GitHub Apps** with least-privilege permission sets (builder, reviewer, watcher, board)
- **4 GHA workflows** (orchestrator, settlement, failure detection, receipt poster)
- **Agent scaffold** (AGENT.md templates for each role)
- **JSONL metrics schema** (run-record v6, cost accounting, settlement events)

## Plugins

Core is intentionally minimal. Project-specific behaviour lives in plugins:

```yaml
plugins:
  - name: three-questions
    source: github:open-agentos/github-agentOS-spec//plugins/three-questions@v1.0
```

The `three-questions` reference plugin ships with this repo and demonstrates the plugin interface.

## Documentation

- [Getting Started](docs/getting-started.md) — full 30-minute walkthrough
- [Label Model](docs/label-model.md) — all axes, routing table, state machine
- [Agent Roles](docs/agent-roles.md) — permissions, runtime interface, branch convention
- [Projects v2 Integration](docs/projects-v2-integration.md) — board fields, fingerprinting
- [Metrics Schema](docs/metrics-schema.md) — JSONL v6 run-record reference
- [Plugin Authoring](docs/plugins.md) — how to build and publish a plugin
- [Specification](SPEC.md) — the full normative document

## Versioning

This spec uses semantic versioning. Breaking changes (label renames, field removals) bump the major
version and ship a `MIGRATION.md`. See [CHANGELOG.md](CHANGELOG.md).

## Reference Implementation

[3qs-ops](https://github.com/mattmcalister/3qs-ops) is the production system that this spec was
extracted from. It runs the [3Qs](https://github.com/mattmcalister/3qs-repo) product and has
processed 140+ agent runs to date.

## License

MIT — see [LICENSE](LICENSE).
