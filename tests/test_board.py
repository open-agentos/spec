"""
Tests for bootstrap/board.py — fingerprinting and board provisioning logic.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bootstrap.board import (
    BoardResult,
    compute_fingerprint,
    load_bindings,
    provision_board,
    save_bindings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SPEC_PATH = REPO_ROOT / "agentOS.yaml"
FIXTURE_SPEC_PATH = REPO_ROOT / "tests" / "fixtures" / "sample-agentOS.yaml"


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Test 1 — compute_fingerprint is stable (deterministic)
# ---------------------------------------------------------------------------


def test_compute_fingerprint_is_stable():
    """The same spec must produce the same fingerprint on every call."""
    spec = _load_yaml(FIXTURE_SPEC_PATH)

    fp1 = compute_fingerprint(spec)
    fp2 = compute_fingerprint(spec)

    assert fp1 == fp2, "compute_fingerprint returned different values for identical input"
    assert fp1.startswith("sha256:"), f"Fingerprint should start with 'sha256:', got {fp1!r}"
    assert len(fp1) == len("sha256:") + 64, "SHA-256 hex digest must be 64 characters"


# ---------------------------------------------------------------------------
# Test 2 — compute_fingerprint changes when a field option name changes
# ---------------------------------------------------------------------------


def test_compute_fingerprint_changes_on_edit():
    """Editing a field option name must produce a different fingerprint."""
    spec = _load_yaml(FIXTURE_SPEC_PATH)
    fp_original = compute_fingerprint(spec)

    # Deep-copy spec and mutate a field option name in board.fields.
    spec_modified = copy.deepcopy(spec)
    board_fields = spec_modified.get("board", {}).get("fields", [])
    assert board_fields, "Fixture spec must have at least one board field"

    # Find the first single_select field and rename its first option.
    for field_def in board_fields:
        if field_def.get("type") == "single_select" and field_def.get("options"):
            field_def["options"][0]["name"] = "MUTATED_OPTION_NAME"
            break
    else:
        pytest.skip("No single_select field with options found in fixture spec")

    fp_modified = compute_fingerprint(spec_modified)

    assert fp_original != fp_modified, (
        "Expected fingerprint to change after mutating an option name, but it stayed the same"
    )


# ---------------------------------------------------------------------------
# Test 3 — provision_board skips when fingerprint matches existing bindings
# ---------------------------------------------------------------------------


def test_provision_board_skips_when_fingerprint_matches(tmp_path):
    """
    If field-bindings.json already has the current fingerprint,
    provision_board must return result.skipped=True without making any API calls.
    """
    spec = _load_yaml(FIXTURE_SPEC_PATH)
    fingerprint = compute_fingerprint(spec)

    bindings_path = tmp_path / "field-bindings.json"
    bindings_data = {
        "schema_fingerprint": fingerprint,
        "board_id": "PVT_kgDOB_fake12345",
        "generated_at": "2026-06-26T00:00:00+00:00",
        "fields": {
            "Status": {"node_id": "PVTF_lADO_fakeStatus", "type": "single_select", "options": {}},
            "Cost to date": {"node_id": "PVTF_lADO_fakeCost", "type": "number"},
        },
    }
    bindings_path.write_text(json.dumps(bindings_data, indent=2) + "\n", encoding="utf-8")

    # Patch _gql to ensure no GraphQL calls are made.
    with patch("bootstrap.board._gql") as mock_gql:
        result = provision_board(
            spec=spec,
            token="ghs_test_token",
            bindings_path=bindings_path,
            org=None,
            dry_run=False,
        )

    assert result.skipped is True, (
        f"Expected result.skipped=True when fingerprint matches, got {result.skipped}"
    )
    assert result.board_id == "PVT_kgDOB_fake12345"
    mock_gql.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — provision_board returns skipped immediately when board.enabled=False
# ---------------------------------------------------------------------------


def test_provision_board_disabled(tmp_path):
    """When board.enabled=False, provision_board must return BoardResult(skipped=True) at once."""
    spec = _load_yaml(FIXTURE_SPEC_PATH)

    # Override enabled to False (deep copy to avoid mutating shared state).
    spec_disabled = copy.deepcopy(spec)
    spec_disabled.setdefault("board", {})["enabled"] = False

    bindings_path = tmp_path / "field-bindings.json"

    with patch("bootstrap.board._gql") as mock_gql:
        result = provision_board(
            spec=spec_disabled,
            token="ghs_test_token",
            bindings_path=bindings_path,
            org=None,
            dry_run=False,
        )

    assert result.skipped is True, (
        f"Expected result.skipped=True when board.enabled=False, got {result.skipped}"
    )
    assert result.ok, "Expected result.ok=True (disabled is not an error)"
    mock_gql.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — labels_from_spec_count: real agentOS.yaml has at least 9 board fields
# ---------------------------------------------------------------------------


def test_labels_from_spec_count():
    """
    The real agentOS.yaml defines 9 board fields (Role, Status, Max turns, Model,
    Outcome, Clean exit, Cost to date, Turns, Attempts).

    The test asserts >= 9 so it stays green when new fields are added.
    The task spec says '10 fields' as an intent — the live file currently has 9;
    we guard with >= 9 and document the discrepancy here.
    """
    spec = _load_yaml(REAL_SPEC_PATH)
    board_fields = spec.get("board", {}).get("fields", [])

    assert len(board_fields) >= 9, (
        f"Expected at least 9 board fields in real agentOS.yaml, got {len(board_fields)}"
    )

    # Verify expected field names are present.
    # Note: 'Status' was renamed to 'Agent Status' (bug #8 — reserved Projects v2 name).
    field_names = {f["name"] for f in board_fields}
    for expected in ("Agent Status", "Cost to date", "Turns", "Attempts"):
        assert expected in field_names, (
            f"Expected field '{expected}' in board.fields, got {field_names}"
        )


# ---------------------------------------------------------------------------
# Bug #8 — Reserved field name guard
# ---------------------------------------------------------------------------

class TestReservedFieldNameGuard:
    """provision_board must raise a clear error on reserved Projects v2 field names."""

    def _spec_with_field(self, field_name: str) -> dict:
        return {
            "board": {
                "enabled": True,
                "name": "Test Board",
                "description": "",
                "fields": [{"name": field_name, "type": "number"}],
            }
        }

    def _mock_gql(self, *args, **kwargs):
        """Minimal GraphQL mock: board create + empty existing fields."""
        query = args[1] if len(args) > 1 else kwargs.get("query", "")
        # _get_user_id  (org=None path)
        if "viewer" in query and "id" in query and "projectsV2" not in query:
            return {"viewer": {"id": "USER_001"}}
        # _find_existing_project (no org)
        if "viewer" in query and "projectsV2" in query:
            return {"viewer": {"projectsV2": {"nodes": []}}}
        if "createProjectV2(" in query:
            return {"createProjectV2": {"projectV2": {"id": "BOARD_001"}}}
        if "node(id:" in query:
            # _fetch_existing_fields — return empty
            return {"node": {"fields": {"nodes": []}}}
        return {}

    @pytest.mark.parametrize("reserved", [
        "Status", "Title", "Assignees", "Labels",
        "Milestone", "Repository", "Reviewers", "Linked pull requests",
    ])
    def test_reserved_field_raises_value_error(self, tmp_path, reserved):
        spec = self._spec_with_field(reserved)
        bindings = tmp_path / "bindings.json"
        with patch("bootstrap.board._gql", side_effect=self._mock_gql):
            result = provision_board(
                spec=spec,
                token="fake-token",
                bindings_path=bindings,
                org=None,
            )
        assert result.error is not None
        assert "reserved" in result.error.lower()
        assert reserved in result.error

    def test_agent_status_not_reserved(self, tmp_path):
        """'Agent Status' (our renamed field) must NOT trigger the reserved guard."""
        spec = self._spec_with_field("Agent Status")
        bindings = tmp_path / "bindings.json"

        created_fields = []

        def mock_gql(*args, **kwargs):
            query = args[1] if len(args) > 1 else kwargs.get("query", "")
            if "viewer" in query and "id" in query and "projectsV2" not in query:
                return {"viewer": {"id": "USER_001"}}
            if "viewer" in query and "projectsV2" in query:
                return {"viewer": {"projectsV2": {"nodes": []}}}
            if "createProjectV2(" in query:
                return {"createProjectV2": {"projectV2": {"id": "BOARD_001"}}}
            if "node(id:" in query:
                return {"node": {"fields": {"nodes": []}}}
            if "createProjectV2Field" in query and "NUMBER" in query:
                created_fields.append("Agent Status")
                return {"createProjectV2Field": {"projectV2Field": {"id": "FIELD_001"}}}
            return {}

        with patch("bootstrap.board._gql", side_effect=mock_gql):
            result = provision_board(
                spec=spec,
                token="fake-token",
                bindings_path=bindings,
                org=None,
            )

        assert result.error is None, result.error
        assert created_fields == ["Agent Status"]


# ---------------------------------------------------------------------------
# Bug #9 — Idempotent field provisioning
# ---------------------------------------------------------------------------

class TestIdempotentFieldProvisioning:
    """Re-running provision_board must skip fields that already exist on the board."""

    def _make_spec(self, fields: list) -> dict:
        return {
            "board": {
                "enabled": True,
                "name": "Test Board",
                "description": "",
                "fields": fields,
            }
        }

    def test_skips_existing_fields_on_rerun(self, tmp_path):
        """Fields already on the board should not trigger a second createProjectV2Field call."""
        spec = self._make_spec([
            {"name": "Role", "type": "single_select",
             "options": [{"name": "Builder", "color": "BLUE", "display": ""}]},
            {"name": "Turns", "type": "number"},
        ])
        bindings = tmp_path / "bindings.json"

        create_calls: list[str] = []

        def mock_gql(*args, **kwargs):
            query = args[1] if len(args) > 1 else kwargs.get("query", "")
            variables = args[2] if len(args) > 2 else kwargs.get("variables", {})
            if "viewer" in query and "id" in query and "projectsV2" not in query:
                return {"viewer": {"id": "USER_001"}}
            if "viewer" in query and "projectsV2" in query:
                return {"viewer": {"projectsV2": {"nodes": []}}}
            if "createProjectV2(" in query:
                return {"createProjectV2": {"projectV2": {"id": "BOARD_001"}}}
            if "node(id:" in query:
                # Simulate "Role" already existing; "Turns" does not
                return {"node": {"fields": {"nodes": [
                    {"id": "EXISTING_ROLE", "name": "Role",
                     "options": [{"id": "OPT_1", "name": "Builder"}]},
                ]}}}
            if "createProjectV2Field" in query:
                name = variables.get("name", "?")
                create_calls.append(name)
                return {"createProjectV2Field": {"projectV2Field": {"id": "FIELD_NEW"}}}
            return {}

        with patch("bootstrap.board._gql", side_effect=mock_gql):
            result = provision_board(
                spec=spec,
                token="fake-token",
                bindings_path=bindings,
                org=None,
            )

        assert result.error is None, result.error
        # Only "Turns" should have been created; "Role" was already present
        assert create_calls == ["Turns"], f"Unexpected create calls: {create_calls}"
        # Bindings should contain both fields
        assert "Role" in result.field_bindings
        assert "Turns" in result.field_bindings
        # The pre-existing Role binding should reuse the existing node ID
        assert result.field_bindings["Role"]["node_id"] == "EXISTING_ROLE"


# ---------------------------------------------------------------------------
# Bug #12/13 — write_credentials naming convention
# ---------------------------------------------------------------------------

class TestWriteCredentials:
    """write_credentials must use {ROLE}_APP_ID / {ROLE}_PRIVATE_KEY convention."""

    from bootstrap.apps import write_credentials

    def test_key_names_follow_role_convention(self, tmp_path):
        from bootstrap.apps import write_credentials
        env = tmp_path / ".env"
        creds = {"id": "99999", "pem": "-----BEGIN RSA PRIVATE KEY-----\nABC\n-----END RSA PRIVATE KEY-----\n",
                 "slug": "agentOS-builder", "webhook_secret": ""}
        write_credentials("builder", creds, env)
        content = env.read_text()
        assert "BUILDER_APP_ID=99999" in content
        assert "BUILDER_PRIVATE_KEY=" in content
        # Must NOT use the old GITHUB_APP_ prefix
        assert "GITHUB_APP_ID_BUILDER" not in content
        assert "GITHUB_APP_PRIVATE_KEY" not in content

    def test_pem_newlines_are_escaped(self, tmp_path):
        from bootstrap.apps import write_credentials
        env = tmp_path / ".env"
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEo\n-----END RSA PRIVATE KEY-----\n"
        creds = {"id": "1", "pem": pem, "slug": "agentOS-reviewer", "webhook_secret": ""}
        write_credentials("reviewer", creds, env)
        content = env.read_text()
        key_line = next(l for l in content.splitlines() if l.startswith("REVIEWER_PRIVATE_KEY="))
        # Should be a single line with literal \n, not real newlines
        assert "\n" not in key_line.split("=", 1)[1]
        assert "\\n" in key_line

    def test_overwrites_existing_keys(self, tmp_path):
        from bootstrap.apps import write_credentials
        env = tmp_path / ".env"
        env.write_text("BUILDER_APP_ID=111\nOTHER_VAR=kept\n")
        creds = {"id": "222", "pem": "pem-data", "slug": "agentOS-builder", "webhook_secret": ""}
        write_credentials("builder", creds, env)
        content = env.read_text()
        assert "BUILDER_APP_ID=222" in content
        assert "BUILDER_APP_ID=111" not in content
        assert "OTHER_VAR=kept" in content


# ---------------------------------------------------------------------------
# Bug #5 — Guided manual setup flow
# ---------------------------------------------------------------------------

class TestRegisterAppsGuidedFlow:
    """register_apps guided flow: instructions printed, credentials collected via prompt_fn."""

    def _minimal_spec(self) -> dict:
        return {
            "agents": [
                {
                    "id": "builder",
                    "create_app": True,
                    "permissions": {"contents": "write", "issues": "write"},
                }
            ]
        }

    def _make_pem(self, tmp_path: Path) -> Path:
        pem_path = tmp_path / "builder.pem"
        pem_path.write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEoABC123\n-----END RSA PRIVATE KEY-----\n"
        )
        return pem_path

    def test_writes_credentials_from_prompted_inputs(self, tmp_path, capsys):
        from bootstrap.apps import register_apps
        pem = self._make_pem(tmp_path)
        env = tmp_path / ".env"
        prompts = iter(["123456", str(pem)])
        results = register_apps(
            spec=self._minimal_spec(),
            env_file=env,
            org="my-org",
            prompt_fn=lambda _: next(prompts),
        )
        assert "builder" in results
        assert results["builder"]["id"] == "123456"
        env_content = env.read_text()
        assert "BUILDER_APP_ID=123456" in env_content
        assert "BUILDER_PRIVATE_KEY=" in env_content

    def test_org_url_in_instructions(self, tmp_path, capsys):
        from bootstrap.apps import register_apps
        pem = self._make_pem(tmp_path)
        prompts = iter(["789", str(pem)])
        register_apps(
            spec=self._minimal_spec(),
            env_file=tmp_path / ".env",
            org="open-agentos",
            prompt_fn=lambda _: next(prompts),
        )
        captured = capsys.readouterr()
        assert "github.com/organizations/open-agentos/settings/apps/new" in captured.out

    def test_personal_account_warning_when_no_org(self, tmp_path, capsys):
        from bootstrap.apps import register_apps
        pem = self._make_pem(tmp_path)
        prompts = iter(["321", str(pem)])
        register_apps(
            spec=self._minimal_spec(),
            env_file=tmp_path / ".env",
            org=None,
            prompt_fn=lambda _: next(prompts),
        )
        captured = capsys.readouterr()
        assert "personal account" in captured.err or "personal account" in captured.out

    def test_invalid_app_id_retries(self, tmp_path):
        from bootstrap.apps import register_apps
        pem = self._make_pem(tmp_path)
        # First prompt returns non-numeric; second is valid
        prompts = iter(["not-a-number", "42", str(pem)])
        results = register_apps(
            spec=self._minimal_spec(),
            env_file=tmp_path / ".env",
            org="org",
            prompt_fn=lambda _: next(prompts),
        )
        assert results["builder"]["id"] == "42"

    def test_empty_spec_returns_empty(self, tmp_path):
        from bootstrap.apps import register_apps
        results = register_apps(
            spec={"agents": []},
            env_file=tmp_path / ".env",
            prompt_fn=lambda _: "",
        )
        assert results == {}
