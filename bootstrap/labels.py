"""
bootstrap/labels.py — Idempotent label sync from agentOS.yaml.

Reads all label axes defined in agentOS.yaml and upserts them to the target
GitHub repository via the Labels REST API.

Behaviour:
  - CREATE  label if it does not exist
  - UPDATE  label colour/description if name exists but values differ
  - SKIP    label if name, colour, and description all match
  - NEVER   delete labels not defined in the spec (user-created labels are preserved)

Public API:
  sync_labels(spec, repo, token, dry_run=False) -> LabelSyncResult

Returns a LabelSyncResult with counts of created / updated / skipped / failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class LabelDef:
    """A single label as parsed from agentOS.yaml."""
    name: str          # Full label name e.g. "status:todo"
    color: str         # Hex without # e.g. "ededed"
    description: str   # GitHub label description (max 100 chars)
    routes_to: Optional[str] = None


@dataclass
class LabelSyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.failed) == 0

    def summary(self) -> str:
        return (
            f"created={len(self.created)} updated={len(self.updated)} "
            f"skipped={len(self.skipped)} failed={len(self.failed)}"
        )


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------

def labels_from_spec(spec: dict[str, Any]) -> list[LabelDef]:
    """Extract all LabelDef objects from a parsed agentOS.yaml dict."""
    result: list[LabelDef] = []
    for axis_block in spec.get("labels", []):
        axis = axis_block["axis"]
        for value in axis_block.get("values", []):
            name = f"{axis}:{value['name']}"
            color = value["color"].lstrip("#")
            description = value.get("description", "")[:100]
            routes_to = value.get("routes_to")
            result.append(LabelDef(name=name, color=color,
                                   description=description, routes_to=routes_to))
    return result


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_existing_labels(repo: str, token: str) -> dict[str, dict]:
    """Return a dict of {label_name: label_object} for all labels in repo."""
    url = f"{GITHUB_API}/repos/{repo}/labels"
    labels: dict[str, dict] = {}
    while url:
        resp = requests.get(url, headers=_headers(token),
                            params={"per_page": 100}, timeout=30)
        resp.raise_for_status()
        for lbl in resp.json():
            labels[lbl["name"]] = lbl
        # Follow pagination Link header
        url = resp.links.get("next", {}).get("url")
    return labels


def _create_label(repo: str, token: str, lbl: LabelDef) -> None:
    resp = requests.post(
        f"{GITHUB_API}/repos/{repo}/labels",
        headers=_headers(token),
        json={"name": lbl.name, "color": lbl.color, "description": lbl.description},
        timeout=30,
    )
    resp.raise_for_status()


def _update_label(repo: str, token: str, lbl: LabelDef) -> None:
    # GitHub requires URL-encoding the label name in the path.
    import urllib.parse
    encoded = urllib.parse.quote(lbl.name, safe="")
    resp = requests.patch(
        f"{GITHUB_API}/repos/{repo}/labels/{encoded}",
        headers=_headers(token),
        json={"color": lbl.color, "description": lbl.description},
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Public sync function
# ---------------------------------------------------------------------------

def sync_labels(
    spec: dict[str, Any],
    repo: str,
    token: str,
    dry_run: bool = False,
) -> LabelSyncResult:
    """Sync all labels from agentOS.yaml to the target repo.

    Args:
        spec:     Parsed agentOS.yaml as a Python dict.
        repo:     "owner/repo" string.
        token:    GitHub token with issues:write scope.
        dry_run:  If True, log what would happen but make no API calls.

    Returns:
        LabelSyncResult with per-category counts.
    """
    result = LabelSyncResult()
    desired = labels_from_spec(spec)

    if not desired:
        log.warning("No labels found in spec — nothing to sync.")
        return result

    log.info("Fetching existing labels from %s …", repo)
    try:
        existing = _fetch_existing_labels(repo, token)
    except requests.HTTPError as exc:
        log.error("Failed to fetch existing labels: %s", exc)
        result.failed.append(("*fetch*", str(exc)))
        return result

    log.info("Found %d existing labels; spec defines %d", len(existing), len(desired))

    for lbl in desired:
        current = existing.get(lbl.name)

        if current is None:
            # Label does not exist — create it.
            action = f"CREATE  {lbl.name}  #{lbl.color}"
            if dry_run:
                log.info("[dry-run] %s", action)
                result.created.append(lbl.name)
                continue
            try:
                _create_label(repo, token, lbl)
                log.info("created: %s", lbl.name)
                result.created.append(lbl.name)
            except requests.HTTPError as exc:
                log.error("failed to create %s: %s", lbl.name, exc)
                result.failed.append((lbl.name, str(exc)))

        else:
            # Label exists — check if update needed.
            current_color = current.get("color", "").lstrip("#").lower()
            current_desc = current.get("description") or ""
            want_color = lbl.color.lower()
            want_desc = lbl.description

            if current_color == want_color and current_desc == want_desc:
                log.debug("skip (identical): %s", lbl.name)
                result.skipped.append(lbl.name)
                continue

            action = f"UPDATE  {lbl.name}  #{current_color}->##{want_color}"
            if dry_run:
                log.info("[dry-run] %s", action)
                result.updated.append(lbl.name)
                continue
            try:
                _update_label(repo, token, lbl)
                log.info("updated: %s", lbl.name)
                result.updated.append(lbl.name)
            except requests.HTTPError as exc:
                log.error("failed to update %s: %s", lbl.name, exc)
                result.failed.append((lbl.name, str(exc)))

    log.info("Label sync complete — %s", result.summary())
    return result
