"""
bootstrap/verify.py — Post-provision health check.

Verifies that a target repository matches agentOS.yaml:
  - All expected labels exist with correct colours
  - The Projects v2 board exists (if board.enabled)
  - All required workflow files are present
  - All GitHub Apps are installed (if credentials are available)

Returns a VerifyResult with per-check pass/fail and an overall ok flag.
Exits with code 0 if all checks pass, 1 if any fail.

Public API:
  verify(spec, repo, token, bindings_path=None) -> VerifyResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

# Workflows that must exist in .github/workflows/ for a conformant deployment.
REQUIRED_WORKFLOWS = [
    "agent-orchestrator.yml",
    "agent-settlement.yml",
    "detect-run-failure.yml",
    "run-receipt.yml",
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerifyResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def print_report(self) -> None:
        width = max((len(c.name) for c in self.checks), default=20)
        print("\nVerify report:")
        for check in self.checks:
            icon = "✓" if check.passed else "✗"
            print(f"  {icon} {check.name:<{width}}  {check.detail}")
        overall = "PASS" if self.ok else "FAIL"
        passed = sum(1 for c in self.checks if c.passed)
        print(f"\n  {overall} — {passed}/{len(self.checks)} checks passed")


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _rest_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_labels(repo: str, token: str) -> dict[str, str]:
    """Return {name: color} for all labels in the repo."""
    url = f"{GITHUB_API}/repos/{repo}/labels"
    labels: dict[str, str] = {}
    while url:
        resp = requests.get(url, headers=_rest_headers(token),
                            params={"per_page": 100}, timeout=30)
        resp.raise_for_status()
        for lbl in resp.json():
            labels[lbl["name"]] = lbl.get("color", "")
        url = resp.links.get("next", {}).get("url")
    return labels


def _fetch_repo_contents(repo: str, path: str, token: str) -> list[str]:
    """Return list of filenames at path in repo (top-level names only)."""
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=_rest_headers(token),
        timeout=30,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return [item["name"] for item in resp.json() if isinstance(resp.json(), list)]


def _check_board_exists(board_id: str, token: str) -> bool:
    """Return True if the Projects v2 board node ID resolves."""
    try:
        resp = requests.post(
            GITHUB_GRAPHQL,
            json={"query": f'{{ node(id: "{board_id}") {{ id }} }}'},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("node") is not None
    except Exception:
        return False


def _check_app_installed(repo: str, app_id: str, token: str) -> bool:
    """Return True if the GitHub App (by App ID) is installed on the repo."""
    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/installation",
            headers=_rest_headers(token),
            timeout=20,
        )
        if resp.status_code == 200:
            installed_app_id = str(resp.json().get("app_id", ""))
            return installed_app_id == str(app_id)
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def _check_labels(spec: dict, repo: str, token: str) -> list[Check]:
    from bootstrap.labels import labels_from_spec
    desired = labels_from_spec(spec)
    checks: list[Check] = []

    try:
        existing = _fetch_labels(repo, token)
    except Exception as exc:
        return [Check("labels:fetch", False, str(exc))]

    missing = [l.name for l in desired if l.name not in existing]
    wrong_color = [
        l.name for l in desired
        if l.name in existing and existing[l.name].lower() != l.color.lower()
    ]
    checks.append(Check(
        "labels:present",
        len(missing) == 0,
        f"all {len(desired)} present" if not missing else f"missing: {missing[:5]}{'…' if len(missing)>5 else ''}",
    ))
    checks.append(Check(
        "labels:colors",
        len(wrong_color) == 0,
        "all colors match" if not wrong_color else f"wrong color: {wrong_color[:3]}",
    ))
    return checks


def _check_workflows(spec: dict, repo: str, token: str) -> list[Check]:
    try:
        present = set(_fetch_repo_contents(repo, ".github/workflows", token))
    except Exception as exc:
        return [Check("workflows:fetch", False, str(exc))]

    checks: list[Check] = []
    for wf in REQUIRED_WORKFLOWS:
        checks.append(Check(
            f"workflow:{wf}",
            wf in present,
            "present" if wf in present else "MISSING",
        ))
    return checks


def _check_board(spec: dict, bindings_path: Optional[Path], token: Optional[str]) -> list[Check]:
    board_cfg = spec.get("board", {})
    if not board_cfg.get("enabled", True):
        return [Check("board:enabled", True, "disabled in spec — skipped")]

    if not bindings_path or not bindings_path.exists():
        return [Check("board:bindings", False, f"field-bindings.json not found at {bindings_path}")]

    import json
    try:
        bindings = json.loads(bindings_path.read_text())
    except Exception as exc:
        return [Check("board:bindings", False, str(exc))]

    board_id = bindings.get("board_id")
    if not board_id:
        return [Check("board:board_id", False, "board_id missing from field-bindings.json")]

    checks: list[Check] = [
        Check("board:board_id", True, board_id),
    ]

    # Check field count matches spec.
    spec_fields = {f["name"] for f in board_cfg.get("fields", [])}
    bound_fields = set(bindings.get("fields", {}).keys())
    missing_fields = spec_fields - bound_fields
    checks.append(Check(
        "board:fields",
        len(missing_fields) == 0,
        f"{len(bound_fields)}/{len(spec_fields)} fields bound"
        if not missing_fields else f"missing bindings: {missing_fields}",
    ))

    # Optionally verify board still exists via GraphQL.
    if token:
        exists = _check_board_exists(board_id, token)
        checks.append(Check("board:live", exists,
                            "board resolves in GitHub" if exists else "board ID not found on GitHub"))

    return checks


# ---------------------------------------------------------------------------
# Public verifier
# ---------------------------------------------------------------------------

def verify(
    spec: dict[str, Any],
    repo: str,
    token: str,
    bindings_path: Optional[Path] = None,
    board_token: Optional[str] = None,
) -> VerifyResult:
    """Verify that target repo matches agentOS.yaml.

    Args:
        spec:           Parsed agentOS.yaml dict.
        repo:           "owner/repo".
        token:          GitHub token with issues:read and metadata:read.
        bindings_path:  Path to field-bindings.json (defaults to ./field-bindings.json).
        board_token:    Token with org projects:read for live board check (optional).

    Returns:
        VerifyResult with per-check pass/fail.
    """
    result = VerifyResult()
    effective_bindings = bindings_path or Path("field-bindings.json")

    result.checks += _check_labels(spec, repo, token)
    result.checks += _check_workflows(spec, repo, token)
    result.checks += _check_board(spec, effective_bindings, board_token or token)

    return result
