"""
bootstrap/board.py — GitHub Projects v2 board provisioner.

Creates a Projects v2 board and all field definitions from agentOS.yaml.
Writes a field-bindings.json file mapping field names to live GraphQL node IDs.
Uses schema fingerprinting to detect drift and skip unnecessary re-provisioning.

Public API:
  provision_board(spec, token, org, repo, bindings_path, dry_run=False) -> BoardResult

Requires: requests
GraphQL mutations use the GitHub Projects v2 API (api.github.com/graphql).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

GITHUB_GRAPHQL = "https://api.github.com/graphql"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class BoardResult:
    board_id: Optional[str] = None
    field_bindings: dict[str, Any] = field(default_factory=dict)
    created_fields: list[str] = field(default_factory=list)
    skipped: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def compute_fingerprint(spec: dict[str, Any]) -> str:
    """SHA-256 of the board.fields block (canonical JSON). Used to detect drift."""
    board = spec.get("board", {})
    fields_block = board.get("fields", [])
    canonical = json.dumps(fields_block, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_bindings(path: Path) -> Optional[dict[str, Any]]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_bindings(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info("Wrote field bindings to %s", path)


# ---------------------------------------------------------------------------
# GraphQL helpers
# ---------------------------------------------------------------------------

def _gql(token: str, query: str, variables: Optional[dict] = None) -> dict:
    resp = requests.post(
        GITHUB_GRAPHQL,
        json={"query": query, "variables": variables or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def _get_org_id(token: str, org: str) -> str:
    data = _gql(token, """
        query($login: String!) {
            organization(login: $login) { id }
        }
    """, {"login": org})
    return data["organization"]["id"]


def _get_user_id(token: str) -> str:
    data = _gql(token, "query { viewer { id } }")
    return data["viewer"]["id"]


def _create_project(token: str, owner_id: str, title: str, description: str) -> str:
    """Create a new Projects v2 board. Returns the board node ID."""
    data = _gql(token, """
        mutation($ownerId: ID!, $title: String!) {
            createProjectV2(input: {ownerId: $ownerId, title: $title}) {
                projectV2 { id }
            }
        }
    """, {"ownerId": owner_id, "title": title})
    board_id = data["createProjectV2"]["projectV2"]["id"]
    log.info("Created board '%s' -> %s", title, board_id)
    return board_id


def _find_existing_project(token: str, org: Optional[str], title: str) -> Optional[str]:
    """Return node ID of an existing project with this title, or None."""
    if org:
        data = _gql(token, """
            query($org: String!, $first: Int!) {
                organization(login: $org) {
                    projectsV2(first: $first) {
                        nodes { id title }
                    }
                }
            }
        """, {"org": org, "first": 20})
        projects = data["organization"]["projectsV2"]["nodes"]
    else:
        data = _gql(token, """
            query($first: Int!) {
                viewer {
                    projectsV2(first: $first) {
                        nodes { id title }
                    }
                }
            }
        """, {"first": 20})
        projects = data["viewer"]["projectsV2"]["nodes"]

    for p in projects:
        if p["title"] == title:
            return p["id"]
    return None


def _create_single_select_field(token: str, board_id: str, name: str,
                                 options: list[dict]) -> dict[str, Any]:
    """Create a single_select field. Returns {node_id, options: {name: id}}."""
    gql_options = [
        {"name": opt["name"], "color": opt["color"], "description": opt.get("display", "")}
        for opt in options
    ]
    data = _gql(token, """
        mutation($projectId: ID!, $name: String!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
            createProjectV2Field(input: {
                projectId: $projectId,
                dataType: SINGLE_SELECT,
                name: $name,
                singleSelectOptions: $options
            }) {
                projectV2Field {
                    ... on ProjectV2SingleSelectField {
                        id
                        options { id name }
                    }
                }
            }
        }
    """, {"projectId": board_id, "name": name, "options": gql_options})
    field_data = data["createProjectV2Field"]["projectV2Field"]
    option_map = {opt["name"]: opt["id"] for opt in field_data.get("options", [])}
    return {"node_id": field_data["id"], "type": "single_select", "options": option_map}


def _create_number_field(token: str, board_id: str, name: str) -> dict[str, Any]:
    """Create a number field. Returns {node_id}."""
    data = _gql(token, """
        mutation($projectId: ID!, $name: String!) {
            createProjectV2Field(input: {
                projectId: $projectId,
                dataType: NUMBER,
                name: $name
            }) {
                projectV2Field {
                    ... on ProjectV2Field { id }
                }
            }
        }
    """, {"projectId": board_id, "name": name})
    node_id = data["createProjectV2Field"]["projectV2Field"]["id"]
    return {"node_id": node_id, "type": "number"}


# ---------------------------------------------------------------------------
# Public provisioner
# ---------------------------------------------------------------------------

def provision_board(
    spec: dict[str, Any],
    token: str,
    bindings_path: Path,
    org: Optional[str] = None,
    dry_run: bool = False,
) -> BoardResult:
    """Provision the Projects v2 board from agentOS.yaml.

    Args:
        spec:           Parsed agentOS.yaml dict.
        token:          GitHub token with organization_projects:write scope (board role).
        bindings_path:  Path to write field-bindings.json.
        org:            GitHub org login (None = personal account).
        dry_run:        If True, log what would happen but make no API calls.

    Returns:
        BoardResult with board_id and field_bindings on success.
    """
    board_cfg = spec.get("board", {})
    if not board_cfg.get("enabled", True):
        log.info("Board provisioning disabled in spec.")
        return BoardResult(skipped=True)

    board_title = board_cfg.get("name", "AgentOS Command Center")
    board_desc = board_cfg.get("description", "Agent telemetry and task board")
    fields_spec = board_cfg.get("fields", [])

    # Fingerprint check — skip if already in sync.
    fingerprint = compute_fingerprint(spec)
    existing_bindings = load_bindings(bindings_path)
    if existing_bindings and existing_bindings.get("schema_fingerprint") == fingerprint:
        log.info("Board fingerprint matches — skipping re-provisioning.")
        return BoardResult(
            board_id=existing_bindings.get("board_id"),
            field_bindings=existing_bindings.get("fields", {}),
            skipped=True,
        )

    if dry_run:
        log.info("[dry-run] Would provision board '%s' with %d fields", board_title, len(fields_spec))
        return BoardResult(board_id="(dry-run)", skipped=False)

    result = BoardResult()

    try:
        # Find or create the board.
        log.info("Looking for existing board '%s'…", board_title)
        board_id = _find_existing_project(token, org, board_title)
        if board_id:
            log.info("Found existing board %s", board_id)
        else:
            log.info("Creating board '%s'…", board_title)
            if org:
                owner_id = _get_org_id(token, org)
            else:
                owner_id = _get_user_id(token)
            board_id = _create_project(token, owner_id, board_title, board_desc)

        result.board_id = board_id

        # Provision fields.
        field_bindings: dict[str, Any] = {}
        for field_def in fields_spec:
            name = field_def["name"]
            ftype = field_def["type"]
            log.info("Provisioning field '%s' (%s)…", name, ftype)
            try:
                if ftype == "single_select":
                    options = field_def.get("options", [])
                    binding = _create_single_select_field(token, board_id, name, options)
                elif ftype == "number":
                    binding = _create_number_field(token, board_id, name)
                else:
                    log.warning("Unsupported field type '%s' for '%s' — skipping", ftype, name)
                    continue
                field_bindings[name] = binding
                result.created_fields.append(name)
                log.info("  -> %s", binding["node_id"])
            except Exception as exc:
                log.error("Failed to create field '%s': %s", name, exc)
                result.error = f"Field '{name}': {exc}"
                return result

        result.field_bindings = field_bindings

        # Write bindings file.
        bindings_data = {
            "schema_fingerprint": fingerprint,
            "board_id": board_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fields": field_bindings,
        }
        save_bindings(bindings_path, bindings_data)

    except Exception as exc:
        log.error("Board provisioning failed: %s", exc)
        result.error = str(exc)

    return result
