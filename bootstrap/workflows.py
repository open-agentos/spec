"""
bootstrap/workflows.py — GHA workflow template copier.

Copies workflow templates from the spec repo's templates/workflows/ directory
into the target repo's .github/workflows/, expanding template variables as it goes.

Template variables (in workflow YAML files, written as {{VARIABLE}}):
  {{AGENT_RUNNER}}      The runner command from runtime.runner in agentOS.yaml
  {{MAX_TURNS}}         runtime.max_turns_default
  {{BRANCH_CONVENTION}} runtime.branch_convention
  {{SPEC_REPO}}         The open-agentos/agentos repo reference
  {{SPEC_VERSION}}      specVersion from agentOS.yaml

Idempotency:
  - If the target file does not exist: copy it.
  - If the target file exists and hash matches: skip.
  - If the target file exists and hash differs: skip UNLESS --force is passed.
    (Operators may have customised their workflows; we never silently overwrite.)

Public API:
  copy_workflows(spec, templates_dir, target_repo_path, force=False, dry_run=False) -> WorkflowResult
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class WorkflowResult:
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skipped_modified: list[str] = field(default_factory=list)   # exists but differs, no --force
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.failed) == 0

    def summary(self) -> str:
        return (
            f"copied={len(self.copied)} skipped={len(self.skipped)} "
            f"skipped_modified={len(self.skipped_modified)} failed={len(self.failed)}"
        )


# ---------------------------------------------------------------------------
# Template variable expansion
# ---------------------------------------------------------------------------

def _build_vars(spec: dict[str, Any]) -> dict[str, str]:
    """Build the template variable substitution map from agentOS.yaml."""
    runtime = spec.get("runtime", {})
    return {
        "AGENT_RUNNER": runtime.get("runner", "hermes run"),
        "MAX_TURNS": str(runtime.get("max_turns_default", 40)),
        "BRANCH_CONVENTION": runtime.get("branch_convention", "agent/{role}/{issue_number}-{slug}"),
        "SPEC_REPO": "open-agentos/agentos",
        "SPEC_VERSION": spec.get("specVersion", "1.0"),
        "RECEIPT_MARKER": runtime.get("receipt_comment_marker", "<!-- agentOS:run-receipt -->"),
    }


def _expand(content: str, variables: dict[str, str]) -> str:
    """Replace {{KEY}} placeholders in content with values from variables dict."""
    for key, value in variables.items():
        content = content.replace(f"{{{{{key}}}}}", value)
    # Warn about any unexpanded placeholders.
    remaining = re.findall(r"\{\{[A-Z_]+\}\}", content)
    if remaining:
        log.warning("Unexpanded template variables: %s", remaining)
    return content


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public copy function
# ---------------------------------------------------------------------------

def copy_workflows(
    spec: dict[str, Any],
    templates_dir: Path,
    target_dir: Path,
    force: bool = False,
    dry_run: bool = False,
) -> WorkflowResult:
    """Copy and expand workflow templates into target_dir/.github/workflows/.

    Args:
        spec:          Parsed agentOS.yaml dict.
        templates_dir: Path to the spec repo's templates/workflows/ directory.
        target_dir:    Root of the target repository.
        force:         Overwrite existing modified workflows.
        dry_run:       Log only, no file writes.

    Returns:
        WorkflowResult with per-category file names.
    """
    result = WorkflowResult()
    workflow_src = templates_dir / "workflows"
    workflow_dst = target_dir / ".github" / "workflows"

    if not workflow_src.exists():
        log.error("Templates directory not found: %s", workflow_src)
        result.failed.append(("*templates*", f"Not found: {workflow_src}"))
        return result

    variables = _build_vars(spec)
    workflow_files = sorted(workflow_src.glob("*.yml")) + sorted(workflow_src.glob("*.yaml"))

    if not workflow_files:
        log.warning("No workflow templates found in %s", workflow_src)
        return result

    if not dry_run:
        workflow_dst.mkdir(parents=True, exist_ok=True)

    for src_file in workflow_files:
        filename = src_file.name
        dst_file = workflow_dst / filename

        try:
            raw = src_file.read_text(encoding="utf-8")
            expanded = _expand(raw, variables)
            expanded_hash = _file_hash(expanded)

            if dst_file.exists():
                existing = dst_file.read_text(encoding="utf-8")
                existing_hash = _file_hash(existing)
                if existing_hash == expanded_hash:
                    log.debug("skip (identical): %s", filename)
                    result.skipped.append(filename)
                    continue
                if not force:
                    log.info("skip (modified, use --force to overwrite): %s", filename)
                    result.skipped_modified.append(filename)
                    continue
                # force=True — overwrite
                log.info("overwrite (--force): %s", filename)
            else:
                log.info("copy: %s -> %s", filename, dst_file)

            if dry_run:
                log.info("[dry-run] would write %s", dst_file)
                result.copied.append(filename)
                continue

            dst_file.write_text(expanded, encoding="utf-8")
            result.copied.append(filename)

        except Exception as exc:
            log.error("Failed to copy %s: %s", filename, exc)
            result.failed.append((filename, str(exc)))

    log.info("Workflow copy complete — %s", result.summary())
    return result


def copy_agent_scaffold(
    spec: dict[str, Any],
    templates_dir: Path,
    target_dir: Path,
    force: bool = False,
    dry_run: bool = False,
) -> WorkflowResult:
    """Copy agent scaffold files (AGENTS.md, agents/ directory) to target repo.

    Copies:
      templates/AGENTS.md           -> target/AGENTS.md
      templates/config.yaml.example -> target/config.yaml.example
      templates/agents/             -> target/agents/
    """
    result = WorkflowResult()
    variables = _build_vars(spec)

    # Files to copy: (source_relative_to_templates, dest_relative_to_target)
    copy_pairs = [
        ("AGENTS.md", "AGENTS.md"),
        ("config.yaml.example", "config.yaml.example"),
    ]
    for src_rel, dst_rel in copy_pairs:
        src = templates_dir / src_rel
        dst = target_dir / dst_rel
        if not src.exists():
            log.debug("Template not found, skipping: %s", src)
            continue
        try:
            raw = src.read_text(encoding="utf-8")
            expanded = _expand(raw, variables)
            if dst.exists() and not force:
                result.skipped_modified.append(dst_rel)
                continue
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(expanded, encoding="utf-8")
            result.copied.append(dst_rel)
            log.info("copied scaffold: %s", dst_rel)
        except Exception as exc:
            result.failed.append((dst_rel, str(exc)))

    # Copy agents/ directory tree.
    agents_src = templates_dir / "agents"
    agents_dst = target_dir / "agents"
    if agents_src.exists():
        for src_file in agents_src.rglob("*"):
            if src_file.is_dir():
                continue
            rel = src_file.relative_to(agents_src)
            dst_file = agents_dst / rel
            try:
                raw = src_file.read_text(encoding="utf-8")
                expanded = _expand(raw, variables)
                if dst_file.exists() and not force:
                    result.skipped_modified.append(str(rel))
                    continue
                if not dry_run:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    dst_file.write_text(expanded, encoding="utf-8")
                result.copied.append(str(rel))
                log.info("copied scaffold: agents/%s", rel)
            except Exception as exc:
                result.failed.append((str(rel), str(exc)))

    # Create ops-metrics/ directory with .gitkeep.
    metrics_dir = target_dir / "ops-metrics"
    gitkeep = metrics_dir / ".gitkeep"
    if not gitkeep.exists():
        if not dry_run:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            gitkeep.write_text("", encoding="utf-8")
        result.copied.append("ops-metrics/.gitkeep")

    return result
