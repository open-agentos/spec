"""
tests/test_apply_dry_run_state.py — Regression tests for dry-run state leakage.

Bug: every _step_* function in bootstrap/apply.py called state.mark_complete(step)
whenever result.ok was True, regardless of opts.dry_run. Since a dry run never
attempts any writes, it can never fail, so result.ok was always True — meaning
a pure `agentOS apply --dry-run` silently wrote "complete" into
.agentOS-state.json for every step it touched.

Consequence: running `--dry-run` once, then running a real `apply` afterwards,
caused the real run to skip steps it believed were already done (should_run()
returns False for "complete" steps) — so labels/board/workflows/scaffold never
actually got synced on the "real" run.

These tests assert: (1) a dry run never calls state.mark_complete, and
(2) a dry run followed by a real run still executes every step for real.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bootstrap.apply import ApplyOptions, StepOutcome, apply
from bootstrap.board import BoardResult
from bootstrap.labels import LabelSyncResult
from bootstrap.state import BootstrapState
from bootstrap.workflows import WorkflowResult


SPEC = {"specVersion": "1.0.0-alpha", "runtime": {}, "labels": [], "board": {"enabled": False}}


def _opts(tmp_path: Path, **kwargs) -> ApplyOptions:
    return ApplyOptions(
        repo="owner/repo",
        labels_token="ghs_test",
        state_path=tmp_path / ".agentOS-state.json",
        target_dir=tmp_path,
        **kwargs,
    )


def _ok_label_result() -> LabelSyncResult:
    r = LabelSyncResult()
    r.created.append("status:todo")
    return r


def _ok_workflow_result() -> WorkflowResult:
    r = WorkflowResult()
    r.copied.append("agentOS/workflows/agent-orchestrator.yml")
    return r


class TestDryRunDoesNotPersistState:
    """A dry-run apply must never leave 'complete' in the state file."""

    @patch("bootstrap.labels.sync_labels")
    def test_labels_step_dry_run_leaves_state_pending(self, mock_sync, tmp_path):
        mock_sync.return_value = _ok_label_result()

        opts = _opts(tmp_path, dry_run=True, only=["labels"])
        result = apply(spec=SPEC, opts=opts)

        assert result.ok
        assert result.steps["labels"].status == "complete"

        # The state file is the thing that matters: it must NOT say complete,
        # or a later real run will skip this step.
        state = BootstrapState(opts.state_path)
        state.load()
        assert state.step_status("labels") == "pending", (
            "dry-run must not mark the labels step complete in state"
        )

    @patch("bootstrap.workflows.copy_workflows")
    def test_workflows_step_dry_run_leaves_state_pending(self, mock_copy, tmp_path):
        mock_copy.return_value = _ok_workflow_result()

        opts = _opts(tmp_path, dry_run=True, only=["workflows"])
        apply(spec=SPEC, opts=opts)

        state = BootstrapState(opts.state_path)
        state.load()
        assert state.step_status("workflows") == "pending"


class TestDryRunThenRealRunStillExecutes:
    """A --dry-run apply followed by a real apply must still perform the real work."""

    @patch("bootstrap.labels.sync_labels")
    def test_real_run_after_dry_run_still_syncs_labels(self, mock_sync, tmp_path):
        mock_sync.return_value = _ok_label_result()

        state_path = tmp_path / ".agentOS-state.json"

        # 1. Dry run first (as a cautious first-time user would).
        dry_opts = _opts(tmp_path, dry_run=True, only=["labels"])
        dry_opts.state_path = state_path
        apply(spec=SPEC, opts=dry_opts)
        assert mock_sync.call_count == 1
        assert mock_sync.call_args.kwargs["dry_run"] is True

        # 2. Real run second.
        real_opts = _opts(tmp_path, dry_run=False, only=["labels"])
        real_opts.state_path = state_path
        result = apply(spec=SPEC, opts=real_opts)

        # The real call must have actually happened — not been skipped because
        # state thought "labels" was already complete from the dry run.
        assert mock_sync.call_count == 2
        assert mock_sync.call_args.kwargs["dry_run"] is False
        assert result.steps["labels"].status == "complete"
        assert "already complete" not in result.steps["labels"].detail

        state = BootstrapState(state_path)
        state.load()
        assert state.step_status("labels") == "complete"


class TestRealRunStillPersistsState:
    """Sanity check: the fix must not break state-marking for real (non-dry) runs."""

    @patch("bootstrap.labels.sync_labels")
    def test_real_run_marks_state_complete(self, mock_sync, tmp_path):
        mock_sync.return_value = _ok_label_result()

        opts = _opts(tmp_path, dry_run=False, only=["labels"])
        apply(spec=SPEC, opts=opts)

        state = BootstrapState(opts.state_path)
        state.load()
        assert state.step_status("labels") == "complete"

    @patch("bootstrap.labels.sync_labels")
    def test_second_real_run_skips_already_complete_step(self, mock_sync, tmp_path):
        """Existing resume behaviour for real runs must be unaffected."""
        mock_sync.return_value = _ok_label_result()

        opts = _opts(tmp_path, dry_run=False, only=["labels"])
        apply(spec=SPEC, opts=opts)
        assert mock_sync.call_count == 1

        result = apply(spec=SPEC, opts=opts)
        assert mock_sync.call_count == 1, "second real run should skip an already-complete step"
        assert result.steps["labels"].detail == "already complete"
