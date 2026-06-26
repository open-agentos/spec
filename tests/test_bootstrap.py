"""
Tests for bootstrap/state.py and bootstrap/apply.py.

state.py tests verify in-memory + file-based step tracking.
apply.py tests use unittest.mock to stub all step functions and
verify the orchestration logic (all steps, --only, stop-on-failure).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from bootstrap.state import BootstrapState, STEP_NAMES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SPEC_PATH = REPO_ROOT / "tests" / "fixtures" / "sample-agentOS.yaml"

TEST_REPO = "owner/test-repo"
TEST_FINGERPRINT = "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"


def _load_spec() -> dict:
    with open(FIXTURE_SPEC_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state(tmp_path) -> BootstrapState:
    """Return a fresh BootstrapState backed by a temp file."""
    return BootstrapState(tmp_path / ".agentOS-state.json")


# ---------------------------------------------------------------------------
# Tests for state.py
# ---------------------------------------------------------------------------


class TestBootstrapState:

    def test_state_init_creates_pending_steps(self, tmp_state):
        """init() must create an entry for every STEP_NAMES with status=pending."""
        tmp_state.init(repo=TEST_REPO, spec_fingerprint=TEST_FINGERPRINT)

        for step in STEP_NAMES:
            status = tmp_state.step_status(step)
            assert status == "pending", (
                f"Expected step '{step}' to be pending after init(), got '{status}'"
            )

    def test_state_mark_complete(self, tmp_state):
        """mark_complete() must set step status to 'complete'."""
        tmp_state.init(repo=TEST_REPO, spec_fingerprint=TEST_FINGERPRINT)
        step = STEP_NAMES[0]  # e.g. "labels"

        tmp_state.mark_complete(step)

        assert tmp_state.step_status(step) == "complete"

    def test_state_should_run_pending(self, tmp_state):
        """A pending step's should_run() must return True."""
        tmp_state.init(repo=TEST_REPO, spec_fingerprint=TEST_FINGERPRINT)
        step = STEP_NAMES[0]

        assert tmp_state.should_run(step) is True

    def test_state_should_run_complete(self, tmp_state):
        """A completed step's should_run() must return False."""
        tmp_state.init(repo=TEST_REPO, spec_fingerprint=TEST_FINGERPRINT)
        step = STEP_NAMES[0]

        tmp_state.mark_complete(step)

        assert tmp_state.should_run(step) is False

    def test_state_needs_reset_different_repo(self, tmp_state):
        """needs_reset() must return True when repo differs from stored value."""
        tmp_state.init(repo=TEST_REPO, spec_fingerprint=TEST_FINGERPRINT)

        # Different repo — should trigger reset.
        assert tmp_state.needs_reset(
            repo="different-owner/other-repo",
            spec_fingerprint=TEST_FINGERPRINT,
        ) is True

        # Same repo and same fingerprint — should NOT trigger reset.
        assert tmp_state.needs_reset(
            repo=TEST_REPO,
            spec_fingerprint=TEST_FINGERPRINT,
        ) is False

    def test_state_all_complete(self, tmp_state):
        """all_complete must be True only after every step is marked complete."""
        tmp_state.init(repo=TEST_REPO, spec_fingerprint=TEST_FINGERPRINT)

        # Initially not all complete.
        assert tmp_state.all_complete is False

        # Mark all steps complete.
        for step in STEP_NAMES:
            assert tmp_state.all_complete is False
            tmp_state.mark_complete(step)

        assert tmp_state.all_complete is True

    def test_state_persists_to_disk(self, tmp_path):
        """State written to disk can be re-loaded by a new BootstrapState instance."""
        state_path = tmp_path / ".agentOS-state.json"
        s1 = BootstrapState(state_path)
        s1.init(repo=TEST_REPO, spec_fingerprint=TEST_FINGERPRINT)
        s1.mark_complete("labels")

        # Load fresh instance from the same path.
        s2 = BootstrapState(state_path)
        s2.load()

        assert s2.step_status("labels") == "complete"
        assert s2.step_status("board") == "pending"


# ---------------------------------------------------------------------------
# Tests for apply.py
# ---------------------------------------------------------------------------


# Spec-level minimal bootstrap options used in apply tests.
def _make_opts(tmp_path, **kwargs):
    from bootstrap.apply import ApplyOptions

    return ApplyOptions(
        repo=TEST_REPO,
        labels_token="ghs_test_labels",
        board_token="ghs_test_board",
        state_path=tmp_path / ".agentOS-state.json",
        **kwargs,
    )


def _complete_outcome(step_name=""):
    from bootstrap.apply import StepOutcome

    return StepOutcome(status="complete", detail=f"mock ok for {step_name}")


def _failed_outcome(step_name=""):
    from bootstrap.apply import StepOutcome

    return StepOutcome(status="failed", detail=f"mock error in {step_name}")


class TestApply:
    """
    apply() iterates over the module-level _STEPS dict, which holds direct
    references to the step functions captured at import time.  Patching the
    module attributes (bootstrap.apply._step_labels etc.) does NOT update the
    already-built dict.  We therefore patch bootstrap.apply._STEPS as a whole,
    replacing the dict values with MagicMocks while keeping the correct key order.
    """

    @staticmethod
    def _mock_steps(overrides: dict) -> dict:
        """Return a _STEPS-shaped dict with MagicMock values for every step.

        overrides maps step_name -> MagicMock (or callable) to use instead of
        the default complete mock.
        """
        from bootstrap.apply import _STEPS

        mocks = {}
        for name in _STEPS:
            if name in overrides:
                mocks[name] = overrides[name]
            else:
                m = MagicMock(return_value=_complete_outcome(name))
                mocks[name] = m
        return mocks

    def test_apply_runs_all_steps_by_default(self, tmp_path):
        """With no --only/--skip, all five steps should appear in result.steps."""
        import bootstrap.apply as apply_mod
        from bootstrap.apply import apply, _STEPS

        spec = _load_spec()
        opts = _make_opts(tmp_path)

        mock_steps = self._mock_steps({})

        with patch.object(apply_mod, "_STEPS", mock_steps):
            result = apply(spec=spec, opts=opts)

        assert result.ok, f"Expected apply to succeed, errors: {result.errors}"
        for step in _STEPS:
            assert step in result.steps, f"Step '{step}' missing from result.steps"
            mock_steps[step].assert_called_once()

    def test_apply_only_flag(self, tmp_path):
        """With only=['labels'], only the labels step should be executed."""
        import bootstrap.apply as apply_mod
        from bootstrap.apply import apply

        spec = _load_spec()
        opts = _make_opts(tmp_path, only=["labels"])

        mock_steps = self._mock_steps({})

        with patch.object(apply_mod, "_STEPS", mock_steps):
            result = apply(spec=spec, opts=opts)

        mock_steps["labels"].assert_called_once()
        for step in ("board", "workflows", "scaffold", "apps"):
            mock_steps[step].assert_not_called()

        assert result.steps["labels"].status == "complete"
        # Non-selected steps should be recorded as skipped.
        for step in ("board", "workflows", "scaffold", "apps"):
            assert result.steps[step].status == "skipped", (
                f"Expected step '{step}' to be skipped, got {result.steps[step].status!r}"
            )

    def test_apply_stops_on_failure(self, tmp_path):
        """If labels step fails, subsequent steps must not be executed."""
        import bootstrap.apply as apply_mod
        from bootstrap.apply import apply

        spec = _load_spec()
        opts = _make_opts(tmp_path)

        failed_labels = MagicMock(return_value=_failed_outcome("labels"))
        mock_steps = self._mock_steps({"labels": failed_labels})

        with patch.object(apply_mod, "_STEPS", mock_steps):
            result = apply(spec=spec, opts=opts)

        assert not result.ok, "Expected apply to fail when labels step fails"
        failed_labels.assert_called_once()

        # Steps after labels should not have been called.
        for step in ("board", "workflows", "scaffold", "apps"):
            mock_steps[step].assert_not_called()

        assert result.steps["labels"].status == "failed"
        assert len(result.errors) >= 1
