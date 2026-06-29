"""
bootstrap/apply.py — Orchestrates all provisioning steps.

Runs the full agentOS apply sequence against a target GitHub repository:
  1. labels      — sync all labels from agentOS.yaml
  2. board       — provision Projects v2 board + fields
  3. workflows   — copy GHA workflow templates
  4. scaffold    — copy AGENTS.md, agents/, config.yaml.example, ops-metrics/
  5. instrument  — add managed-block markers to provisioned files (for --upgrade)
  6. apps        — (skipped here; run via `agentOS setup` interactively)

Progress is tracked in .agentOS-state.json. Re-running resumes from the last
failed step. Pass --reset to start fresh.

Public API:
  apply(spec, repo, token, board_token, opts) -> ApplyResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from bootstrap.board import compute_fingerprint, provision_board
from bootstrap.labels import sync_labels
from bootstrap.state import BootstrapState
from bootstrap.workflows import copy_agent_scaffold, copy_workflows

log = logging.getLogger(__name__)

# Templates and schema are bundled inside the bootstrap package directory
# (bootstrap/templates/ and bootstrap/schema/) so they are always present
# after a pip install without needing the source tree on disk.
_BOOTSTRAP_DIR = Path(__file__).resolve().parent
_BUNDLED_TEMPLATES = _BOOTSTRAP_DIR / "templates"


# ---------------------------------------------------------------------------
# Options + result types
# ---------------------------------------------------------------------------

@dataclass
class ApplyOptions:
    repo: str                                # "owner/repo"
    labels_token: str                        # token with issues:write
    board_token: Optional[str] = None        # token with org projects:write
    org: Optional[str] = None               # GitHub org for board
    target_dir: Optional[Path] = None        # local checkout of target repo
    templates_dir: Optional[Path] = None     # override templates source
    bindings_path: Optional[Path] = None     # override field-bindings.json location
    state_path: Optional[Path] = None        # override state file location
    force_workflows: bool = False
    dry_run: bool = False
    reset: bool = False
    only: Optional[list[str]] = None         # run only these steps
    skip: Optional[list[str]] = None         # skip these steps
    instrument_only: bool = False            # run only the instrument step


@dataclass
class StepOutcome:
    status: str   # complete | skipped | failed
    detail: str = ""


@dataclass
class ApplyResult:
    steps: dict[str, StepOutcome] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.status in ("complete", "skipped") for s in self.steps.values())

    def print_summary(self) -> None:
        print("\nApply summary:")
        for step, outcome in self.steps.items():
            icon = "✓" if outcome.status == "complete" else ("~" if outcome.status == "skipped" else "✗")
            print(f"  {icon} {step:<12} {outcome.status}  {outcome.detail}")
        if self.errors:
            print("\nErrors:")
            for err in self.errors:
                print(f"  • {err}")


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _step_labels(spec: dict, opts: ApplyOptions, state: BootstrapState) -> StepOutcome:
    from bootstrap.labels import sync_labels
    result = sync_labels(
        spec=spec,
        repo=opts.repo,
        token=opts.labels_token,
        dry_run=opts.dry_run,
    )
    if result.ok:
        detail = result.summary()
        state.mark_complete("labels")
        return StepOutcome("complete", detail)
    else:
        errs = "; ".join(f[1] for f in result.failed)
        state.mark_failed("labels", errs)
        return StepOutcome("failed", errs)


def _step_board(spec: dict, opts: ApplyOptions, state: BootstrapState) -> StepOutcome:
    if not opts.board_token:
        msg = "No board token — set board_token in options or run `agentOS setup` first"
        state.mark_skipped("board", msg)
        return StepOutcome("skipped", msg)

    bindings_path = opts.bindings_path or Path("field-bindings.json")
    result = provision_board(
        spec=spec,
        token=opts.board_token,
        bindings_path=bindings_path,
        org=opts.org,
        dry_run=opts.dry_run,
    )
    if result.skipped:
        state.mark_skipped("board", "fingerprint match")
        return StepOutcome("skipped", "fingerprint match — already in sync")
    if result.ok:
        detail = f"board={result.board_id} fields={len(result.created_fields)}"
        state.mark_complete("board")
        return StepOutcome("complete", detail)
    else:
        state.mark_failed("board", result.error or "unknown")
        return StepOutcome("failed", result.error or "unknown")


def _step_workflows(spec: dict, opts: ApplyOptions, state: BootstrapState) -> StepOutcome:
    templates_dir = opts.templates_dir or _BUNDLED_TEMPLATES
    target_dir = opts.target_dir or Path.cwd()
    result = copy_workflows(
        spec=spec,
        templates_dir=templates_dir,
        target_dir=target_dir,
        force=opts.force_workflows,
        dry_run=opts.dry_run,
    )
    if result.ok:
        detail = result.summary()
        state.mark_complete("workflows")
        return StepOutcome("complete", detail)
    else:
        errs = "; ".join(f[1] for f in result.failed)
        state.mark_failed("workflows", errs)
        return StepOutcome("failed", errs)


def _step_scaffold(spec: dict, opts: ApplyOptions, state: BootstrapState) -> StepOutcome:
    templates_dir = opts.templates_dir or _BUNDLED_TEMPLATES
    target_dir = opts.target_dir or Path.cwd()
    result = copy_agent_scaffold(
        spec=spec,
        templates_dir=templates_dir,
        target_dir=target_dir,
        force=opts.force_workflows,
        dry_run=opts.dry_run,
    )
    if result.ok:
        detail = result.summary()
        state.mark_complete("scaffold")
        return StepOutcome("complete", detail)
    else:
        errs = "; ".join(f[1] for f in result.failed)
        state.mark_failed("scaffold", errs)
        return StepOutcome("failed", errs)


def _step_instrument(spec: dict, opts: ApplyOptions, state: BootstrapState) -> StepOutcome:
    """Add managed-block markers to provisioned files so --upgrade can process them.

    This step is idempotent: files that already have markers are left untouched.
    Files that were written by the scaffold/workflows steps get their entire
    spec-owned content wrapped in a managed block with a hash.
    """
    from bootstrap.upgrade import instrument_files

    target_dir = opts.target_dir or Path.cwd()
    result = instrument_files(
        target_dir=target_dir,
        dry_run=opts.dry_run,
    )
    if result.ok:
        n = len(result.files_instrumented)
        s = len(result.files_skipped)
        detail = f"instrumented={n} already_marked={s} missing={len(result.files_missing)}"
        state.mark_complete("instrument")
        return StepOutcome("complete", detail)
    else:
        errs = "; ".join(result.errors)
        state.mark_failed("instrument", errs)
        return StepOutcome("failed", errs)


def _step_apps(_spec: dict, opts: ApplyOptions, state: BootstrapState) -> StepOutcome:
    """Apps are registered interactively via `agentOS setup`, not here."""
    msg = f"run `agentOS setup --repo {opts.repo}` to register GitHub Apps"
    state.mark_skipped("apps", msg)
    return StepOutcome("skipped", msg)


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------

_STEPS = {
    "labels": _step_labels,
    "board": _step_board,
    "workflows": _step_workflows,
    "scaffold": _step_scaffold,
    "instrument": _step_instrument,
    "apps": _step_apps,
}


def apply(spec: dict[str, Any], opts: ApplyOptions) -> ApplyResult:
    """Run the full apply sequence, resumable from last failure.

    Args:
        spec:  Parsed agentOS.yaml dict.
        opts:  ApplyOptions controlling tokens, paths, and flags.

    Returns:
        ApplyResult with per-step outcomes.
    """
    result = ApplyResult()

    # --instrument flag: run only the instrument step, skip everything else.
    if opts.instrument_only:
        state_path = opts.state_path or Path(".agentOS-state.json")
        state = BootstrapState(state_path)
        state.load()
        outcome = _step_instrument(spec, opts, state)
        result.steps["instrument"] = outcome
        if outcome.status == "failed":
            result.errors.append(f"instrument: {outcome.detail}")
        return result

    # State tracking.
    state_path = opts.state_path or Path(".agentOS-state.json")
    state = BootstrapState(state_path)
    state.load()

    spec_fingerprint = compute_fingerprint(spec)
    if opts.reset or state.needs_reset(opts.repo, spec_fingerprint):
        log.info("Initialising fresh state (reset=%s)", opts.reset)
        state.init(opts.repo, spec_fingerprint)

    # Determine which steps to run.
    only = set(opts.only) if opts.only else set(_STEPS.keys())
    skip = set(opts.skip) if opts.skip else set()

    for step_name, step_fn in _STEPS.items():
        if step_name not in only or step_name in skip:
            result.steps[step_name] = StepOutcome("skipped", "--only/--skip")
            continue
        if not state.should_run(step_name):
            prev_status = state.step_status(step_name)
            result.steps[step_name] = StepOutcome(prev_status, "already complete")
            continue

        log.info("Running step: %s", step_name)
        outcome = step_fn(spec, opts, state)
        result.steps[step_name] = outcome
        if outcome.status == "failed":
            result.errors.append(f"{step_name}: {outcome.detail}")
            log.error("Step '%s' failed — stopping.", step_name)
            break

    return result


def apply_from_file(
    spec_file: Path,
    repo: str,
    labels_token: str,
    board_token: Optional[str] = None,
    **kwargs,
) -> ApplyResult:
    """Convenience wrapper: load spec from file and call apply()."""
    with open(spec_file, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    opts = ApplyOptions(
        repo=repo,
        labels_token=labels_token,
        board_token=board_token,
        **kwargs,
    )
    return apply(spec, opts)
