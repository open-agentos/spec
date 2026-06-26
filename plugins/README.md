# agentOS Plugin Authoring Guide

Plugins extend agentOS without modifying core spec files. This guide explains
how to write, test, and publish a plugin.


## 1. What is a plugin?

A plugin is a self-contained package that adds capability to an agentOS
installation. It ships its own label axes, project-board fields, GitHub Actions
workflows, and agent configuration files.

Plugins are applied on top of a conforming agentOS.yaml install. They NEVER
modify core spec files — they only add. This means a repo can adopt a plugin,
remove it, or swap it out without breaking the base agentOS contract.

Typical use-cases:
- Domain-specific label axes (e.g. phase tracking, SLA tiers)
- Scheduled agent jobs (e.g. daily watcher, weekly triage)
- Extra board fields (e.g. estimated effort, customer segment)
- Role-specific AGENT.md overrides for non-core roles


## 2. Plugin manifest format (plugin.yaml)

Every plugin must have a plugin.yaml at its repo root (or at the path declared
when referencing the plugin). The schema:

  pluginVersion: <semver>         # required — e.g. 1.0 or 1.2.3
  name: <identifier>              # required — kebab-case, unique per registry
  description: <string>           # required — one-line human description
  specVersionRequired: <range>    # required — semver range, e.g. '>=1.0 <2.0'

  labels:                         # optional — list of label axis definitions
    - axis: <name>
      description: <string>
      values:
        - name: <label-value>
          color: <hex without #>
          description: <string>
          routes_to: <agent-role>   # optional — triggers agent routing

  follow_on_routes:               # optional — extend the core follow-on routing table
    <follow-on-label-value>:
      routes_to: <agent-role>

  board_fields:                   # optional — extra Projects v2 custom fields
    - name: <field-name>
      type: <text|number|date|single_select>
      options:                    # for single_select only
        - <option>

  workflows:                      # optional — workflow files to install
    - source: <path-in-plugin-repo>
      target: <path-in-target-repo>
      enabled_by_default: <bool>

  agents:                         # optional — agent config overrides
    - role: <agent-role>
      config_file: <path-in-plugin-repo>


## 3. What a plugin can add

Labels axes
  A plugin may define entirely new label axes using its own namespace prefix,
  or explicitly extend a core axis (like follow-on) by listing additional
  values. New axes must not collide with core axis names unless they are
  declared as extensions.

Project board fields
  Plugins may add custom fields to the Projects v2 board. Field names are
  namespaced by the plugin (e.g. "3qs:phase") to avoid collisions.

Workflows
  A plugin may ship one or more GitHub Actions workflow files. These are copied
  into .github/workflows/ at install time. Workflow names must be distinct from
  core workflow names.

Agent AGENT.md overrides
  A plugin may supply an AGENT.md.template for any agent role (including
  non-core roles it introduces). These are merged with or replace the base
  AGENT.md during install, according to merge strategy declared in agentOS.yaml.


## 4. What a plugin MUST NOT do

- Modify the color of any core label (status:*, type:*, orchestrator:*,
  follow-on:docs-needed, follow-on:changes-requested). Colors are part of the
  visual contract operators rely on.
- Remove or rename core label axes or values.
- Remove core Projects v2 fields (Title, Status, Assignees, Issue).
- Override core workflow files (agent-orchestrator.yml, agent-settlement.yml,
  run-receipt.yml, detect-run-failure.yml).
- Declare specVersionRequired ranges that would pass validation against an
  incompatible spec version.
- Break conformance of the host repo — after plugin installation the repo must
  still pass `agentOS validate`.


## 5. How to reference a plugin from agentOS.yaml

In agentOS.yaml, add a plugins section:

  plugins:
    - name: three-questions
      source: github:your-org/three-questions-plugin   # owner/repo shorthand
      version: '>=1.0'                                  # semver range
      enabled: true
      config:                                           # plugin-specific config
        watcher_schedule: '0 8 * * *'

The agentOS CLI resolves the source, fetches plugin.yaml from the repo root,
validates the specVersionRequired range, and applies the plugin.

Supported source formats:
  github:<owner>/<repo>           — fetches from GitHub at default branch
  github:<owner>/<repo>@<ref>     — pin to a tag, branch, or commit SHA
  local:./<path>                  — local path, useful during development


## 6. Example: the three-questions reference plugin

The three-questions plugin lives at plugins/three-questions/ in this repo.
It demonstrates:

- A phase label axis (phase:1, phase:2, phase:3) for milestone tracking.
- An extension of the follow-on axis with a dreaming-needed value that routes
  to the watcher agent role.
- A follow_on_routes block that maps docs-needed to the docs agent.
- A scheduled watcher workflow (watcher-schedule.yml) that runs daily at 08:00
  UTC and creates a new GitHub issue containing the three most important
  questions a PM should ask today.
- A watcher agent config (AGENT.md.template) that describes the watcher's
  purpose, sources, output format, and constraints.

See plugins/three-questions/plugin.yaml for the full manifest and
plugins/three-questions/agents/watcher/AGENT.md.template for the agent config.


## 7. Publishing a plugin

A plugin is just a public GitHub repository with a plugin.yaml at a known
path (repo root by default). There is no central registry — plugins are
referenced by their GitHub owner/repo coordinates.

Checklist before publishing:
  [ ] plugin.yaml validates against the plugin manifest schema
  [ ] specVersionRequired accurately reflects the minimum compatible spec version
  [ ] All workflow files have been tested in a sandbox repo
  [ ] Label color values do not conflict with core label colors
  [ ] README.md (in the plugin repo) documents all config options
  [ ] A semver tag has been pushed so users can pin to a stable release

To make your plugin discoverable, add the topic agentOS-plugin to your GitHub
repository. The agentOS community index is maintained at:
https://github.com/agentOS-spec/plugin-registry
