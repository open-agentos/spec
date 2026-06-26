"""
Tests for bootstrap/labels.py — label parsing and GitHub API sync.

Uses the `responses` library to intercept all outbound HTTP so tests run
fully offline.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest
import responses as rsps_lib
import yaml

from bootstrap.labels import LabelDef, LabelSyncResult, labels_from_spec, sync_labels

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SPEC_PATH = REPO_ROOT / "agentOS.yaml"
FIXTURE_SPEC_PATH = REPO_ROOT / "tests" / "fixtures" / "sample-agentOS.yaml"

GITHUB_API = "https://api.github.com"
TEST_REPO = "owner/repo"
TEST_TOKEN = "ghs_testtoken0000000000000000000000000"


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _labels_endpoint(repo: str = TEST_REPO) -> str:
    return f"{GITHUB_API}/repos/{repo}/labels"


# ---------------------------------------------------------------------------
# Test 1 — labels_from_spec parses all axes from real agentOS.yaml
# ---------------------------------------------------------------------------


def test_labels_from_spec_parses_all_axes():
    """Load real agentOS.yaml and verify all label axes are parsed correctly."""
    spec = _load_yaml(REAL_SPEC_PATH)
    labels = labels_from_spec(spec)

    # Real spec defines 20 labels across 6 axes; ensure we parse them all.
    assert len(labels) >= 20, (
        f"Expected at least 20 labels from spec, got {len(labels)}"
    )

    # Verify specific label name and color are preserved.
    label_map = {lbl.name: lbl for lbl in labels}
    assert "status:todo" in label_map, "Expected 'status:todo' in parsed labels"
    assert label_map["status:todo"].color == "ededed", (
        f"Expected color 'ededed' for status:todo, got {label_map['status:todo'].color!r}"
    )

    # Verify label name format is {axis}:{value} for a sampling of axes.
    names = {lbl.name for lbl in labels}
    for expected in ("status:in-review", "agent:builder", "type:feature"):
        assert expected in names, f"Expected '{expected}' in parsed labels"

    # Verify routes_to is wired correctly on status:todo.
    assert label_map["status:todo"].routes_to == "builder"


# ---------------------------------------------------------------------------
# Test 2 — sync_labels creates missing labels
# ---------------------------------------------------------------------------


@rsps_lib.activate
def test_sync_labels_creates_missing():
    """GET returns empty list; POST should be called for every desired label."""
    spec = _load_yaml(FIXTURE_SPEC_PATH)
    desired = labels_from_spec(spec)
    assert desired, "Fixture spec must define at least one label"

    # Mock GET — no existing labels.
    rsps_lib.add(
        rsps_lib.GET,
        _labels_endpoint(),
        json=[],
        status=200,
    )

    # Mock POST for each label creation.
    for lbl in desired:
        rsps_lib.add(
            rsps_lib.POST,
            _labels_endpoint(),
            json={"name": lbl.name, "color": lbl.color, "description": lbl.description},
            status=201,
        )

    result = sync_labels(spec=spec, repo=TEST_REPO, token=TEST_TOKEN, dry_run=False)

    assert result.ok, f"sync_labels failed: {result.failed}"
    assert len(result.created) == len(desired), (
        f"Expected {len(desired)} created, got {len(result.created)}"
    )
    assert result.updated == []
    assert result.skipped == []

    # Verify each desired label name appears in result.created.
    created_set = set(result.created)
    for lbl in desired:
        assert lbl.name in created_set, f"Expected '{lbl.name}' in result.created"


# ---------------------------------------------------------------------------
# Test 3 — sync_labels skips labels that already match
# ---------------------------------------------------------------------------


@rsps_lib.activate
def test_sync_labels_skips_identical():
    """GET returns labels matching spec exactly; nothing should be written."""
    spec = _load_yaml(FIXTURE_SPEC_PATH)
    desired = labels_from_spec(spec)

    # Build a mock GitHub response that exactly matches the spec.
    existing_gh_labels = [
        {
            "name": lbl.name,
            "color": lbl.color,
            "description": lbl.description,
        }
        for lbl in desired
    ]

    rsps_lib.add(
        rsps_lib.GET,
        _labels_endpoint(),
        json=existing_gh_labels,
        status=200,
    )

    result = sync_labels(spec=spec, repo=TEST_REPO, token=TEST_TOKEN, dry_run=False)

    assert result.ok
    assert result.created == [], f"Expected no creates, got {result.created}"
    assert result.updated == [], f"Expected no updates, got {result.updated}"
    assert len(result.skipped) == len(desired), (
        f"Expected {len(desired)} skipped, got {len(result.skipped)}"
    )

    # Every desired label must be in skipped.
    skipped_set = set(result.skipped)
    for lbl in desired:
        assert lbl.name in skipped_set, f"Expected '{lbl.name}' in result.skipped"


# ---------------------------------------------------------------------------
# Test 4 — sync_labels updates a label with the wrong color
# ---------------------------------------------------------------------------


@rsps_lib.activate
def test_sync_labels_updates_wrong_color():
    """GET returns a label with a stale color; PATCH should be called for it."""
    spec = _load_yaml(FIXTURE_SPEC_PATH)
    desired = labels_from_spec(spec)
    assert desired, "Fixture spec must define at least one label"

    # Pick the first label and give it a wrong color in the existing set.
    target = desired[0]
    wrong_color = "ffffff"
    assert wrong_color != target.color, "Test assumption: wrong_color differs from spec color"

    existing_gh_labels = []
    for lbl in desired:
        color = wrong_color if lbl.name == target.name else lbl.color
        existing_gh_labels.append(
            {"name": lbl.name, "color": color, "description": lbl.description}
        )

    rsps_lib.add(
        rsps_lib.GET,
        _labels_endpoint(),
        json=existing_gh_labels,
        status=200,
    )

    encoded_name = urllib.parse.quote(target.name, safe="")
    rsps_lib.add(
        rsps_lib.PATCH,
        f"{GITHUB_API}/repos/{TEST_REPO}/labels/{encoded_name}",
        json={"name": target.name, "color": target.color, "description": target.description},
        status=200,
    )

    result = sync_labels(spec=spec, repo=TEST_REPO, token=TEST_TOKEN, dry_run=False)

    assert result.ok, f"sync_labels failed: {result.failed}"
    assert target.name in result.updated, (
        f"Expected '{target.name}' in result.updated, got {result.updated}"
    )
    assert result.created == []


# ---------------------------------------------------------------------------
# Test 5 — sync_labels dry_run makes no API calls
# ---------------------------------------------------------------------------


@rsps_lib.activate
def test_sync_labels_dry_run():
    """With dry_run=True, sync_labels should not issue any POST or PATCH calls."""
    spec = _load_yaml(FIXTURE_SPEC_PATH)
    desired = labels_from_spec(spec)

    # Existing labels: only half are present (so the other half would trigger creates).
    half = desired[: len(desired) // 2]
    existing_gh_labels = [
        {"name": lbl.name, "color": lbl.color, "description": lbl.description}
        for lbl in half
    ]

    rsps_lib.add(
        rsps_lib.GET,
        _labels_endpoint(),
        json=existing_gh_labels,
        status=200,
    )

    # Deliberately do NOT register any POST or PATCH mocks.
    # If sync_labels issues them, `responses` will raise ConnectionError.

    result = sync_labels(spec=spec, repo=TEST_REPO, token=TEST_TOKEN, dry_run=True)

    assert result.ok, f"dry_run sync_labels failed: {result.failed}"

    # In dry_run mode the code tracks creates/updates without real HTTP writes.
    # The missing half should appear in result.created.
    existing_names = {lbl.name for lbl in half}
    missing = {lbl.name for lbl in desired} - existing_names
    for name in missing:
        assert name in result.created, (
            f"Expected dry-run missing label '{name}' in result.created"
        )

    # Verify that no POST or PATCH calls were actually made.
    for call in rsps_lib.calls:
        assert call.request.method == "GET", (
            f"dry_run=True issued a {call.request.method} request to {call.request.url}"
        )
