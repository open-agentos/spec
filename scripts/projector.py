#!/usr/bin/env python3
"""Projector: reduce RunRecord JSONL -> board telemetry fields.

Two entry points:

  project_provisional(board_token, board_id, issue_number, item_id, bindings_path)
      Called at run-end.  Reads all ops-metrics JSONL lines for the issue,
      reduces them, writes Outcome=Provisional plus telemetry numbers to the
      board.  Best-effort; never raises into the caller.

  settle(board_token, board_id, target_repo, pr_number, merged, bindings_path)
      Called by the settlement workflow on PR close.  Resolves the linked
      issue from the PR body, finds the board item, writes the final Outcome.

The pure core -- reduce_runs() -- is I/O-free and fully unit-testable.
The thin adapters (write_telemetry, settle) do GraphQL via GitHub Projects v2.

Environment variables:
  BOARD_ID      GitHub Projects v2 node ID (used when board_id is not passed
                explicitly and field-bindings.json does not contain it).
  TARGET_REPO   owner/repo of the target (product) repository.
  OPS_REPO      owner/repo of the ops repository (for JSONL corpus location).
"""

from __future__ import annotations

import json
import logging
import os
import re
import argparse
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

OPS_ROOT = Path(__file__).resolve().parent.parent
METRICS_DIR = OPS_ROOT / "ops-metrics"

API = "https://api.github.com"

# ── Pure data type ─────────────────────────────────────────────────────────────

@dataclass
class TelemetryValues:
    """Reduced telemetry for one issue, ready to write to the board."""
    cost_to_date: float = 0.0   # sum of total_cost_usd across all attempts
    attempts: int = 0           # max(identity.attempt)
    turns: int = 0              # execution.turns of the latest run
    clean_exit: str = ""        # clean_exit.status of the latest run
    outcome: str = "Provisional"  # always Provisional from the reducer


# ── Pure reducer ──────────────────────────────────────────────────────────────

def reduce_runs(records: list[dict[str, Any]]) -> TelemetryValues:
    """Reduce a list of RunRecord dicts for one issue into TelemetryValues.

    Rules:
    - cost_to_date = sum(cost.total_cost_usd); null/missing cost -> 0
    - attempts     = max(identity.attempt)
    - turns        = execution.turns  of the record with the latest lifecycle.ended_at
    - clean_exit   = clean_exit.status of the same latest record
    - outcome      = "Provisional" always (settlement writes the final value)

    Empty list -> all defaults, no exception.
    Out-of-order records are handled correctly (sort by ended_at).
    """
    if not records:
        return TelemetryValues()

    cost_total = 0.0
    max_attempt = 0

    for rec in records:
        # cost
        try:
            c = (rec.get("cost") or {}).get("total_cost_usd")
            if c is not None:
                cost_total += float(c)
        except (TypeError, ValueError):
            pass

        # attempt
        try:
            a = (rec.get("identity") or {}).get("attempt")
            if a is not None:
                max_attempt = max(max_attempt, int(a))
        except (TypeError, ValueError):
            pass

    # latest by lifecycle.ended_at (ISO 8601 lexicographic sort is correct)
    def _ended_at(rec: dict) -> str:
        return (rec.get("lifecycle") or {}).get("ended_at") or ""

    latest = max(records, key=_ended_at)

    turns = 0
    try:
        turns = int((latest.get("execution") or {}).get("turns") or 0)
    except (TypeError, ValueError):
        pass

    clean_exit_raw = (latest.get("clean_exit") or {}).get("status") or ""
    # Map raw enum value to the board option name
    _EXIT_MAP = {
        "clean":        "Clean",
        "crashed":      "Crashed",
        "max_turns":    "Max turns",
        "infra_failure":"Infra failure",
    }
    clean_exit = _EXIT_MAP.get(clean_exit_raw.lower(), clean_exit_raw.title() if clean_exit_raw else "")

    return TelemetryValues(
        cost_to_date=round(cost_total, 6),
        attempts=max_attempt,
        turns=turns,
        clean_exit=clean_exit,
        outcome="Provisional",
    )


# ── JSONL reader ──────────────────────────────────────────────────────────────

def _load_records_for_issue(issue_number: int, target_repo: str) -> list[dict[str, Any]]:
    """Read all ops-metrics JSONL lines for a given issue number + repo."""
    records: list[dict[str, Any]] = []
    if not METRICS_DIR.exists():
        return records

    for jsonl_file in sorted(METRICS_DIR.glob("runs-*.jsonl")):
        try:
            for line in jsonl_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                identity = rec.get("identity") or {}
                if (
                    identity.get("number") == issue_number
                    and identity.get("repo") == target_repo
                ):
                    records.append(rec)
        except OSError:
            pass

    return records


# ── GraphQL helpers ───────────────────────────────────────────────────────────

UPDATE_NUMBER_MUTATION = """
mutation UpdateNumber(
  $projectId: ID!,
  $itemId: ID!,
  $fieldId: ID!,
  $value: Float!
) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: { number: $value }
  }) {
    projectV2Item { id }
  }
}
"""

UPDATE_SELECT_MUTATION = """
mutation UpdateSelect(
  $projectId: ID!,
  $itemId: ID!,
  $fieldId: ID!,
  $optionId: String!
) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: { singleSelectOptionId: $optionId }
  }) {
    projectV2Item { id }
  }
}
"""


def _graphql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ops-projector",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            if body.get("errors"):
                LOGGER.warning("GraphQL errors: %s", body["errors"])
            return body
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        LOGGER.warning("GraphQL HTTP %s: %s", exc.code, err[:200])
    except Exception as exc:
        LOGGER.warning("GraphQL error: %s", exc)
    return {}


def _set_number(token: str, board_id: str, item_id: str, field_id: str, value: float) -> None:
    """Write a number field value.  Best-effort."""
    _graphql(token, UPDATE_NUMBER_MUTATION, {
        "projectId": board_id,
        "itemId": item_id,
        "fieldId": field_id,
        "value": value,
    })


def _set_single_select(token: str, board_id: str, item_id: str, field_id: str, option_id: str) -> None:
    """Write a single-select field value.  Best-effort."""
    if not option_id:
        return
    _graphql(token, UPDATE_SELECT_MUTATION, {
        "projectId": board_id,
        "itemId": item_id,
        "fieldId": field_id,
        "optionId": option_id,
    })


# ── Bindings loader ───────────────────────────────────────────────────────────

def _load_bindings(bindings_path: Path | None = None) -> dict:
    """Load field-bindings.json if it exists; return empty dict otherwise."""
    if bindings_path is None:
        bindings_path = OPS_ROOT / "field-bindings.json"
    if not bindings_path.exists():
        LOGGER.debug("field-bindings.json not found at %s; projector is a no-op", bindings_path)
        return {}
    try:
        return json.loads(bindings_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not load field-bindings.json: %s", exc)
        return {}


def _resolve_board_id(bindings: dict) -> str:
    """Resolve the Projects v2 board node ID.

    Precedence:
      1. ``board_id`` key in field-bindings.json
      2. BOARD_ID environment variable
    Returns empty string if neither is set.
    """
    from_bindings = (bindings.get("board_id") or "").strip()
    if from_bindings:
        return from_bindings
    return os.environ.get("BOARD_ID", "").strip()


# ── Telemetry writer ──────────────────────────────────────────────────────────

def write_telemetry(
    board_token: str,
    board_id: str,
    item_id: str,
    values: TelemetryValues,
    bindings: dict,
) -> None:
    """Write TelemetryValues to the board item using field-bindings.json IDs.

    Best-effort: any individual field write failure is logged and skipped;
    remaining fields continue.  Never raises.
    """
    fields = bindings.get("fields", {})

    def _field_id(name: str) -> str:
        return (fields.get(name) or {}).get("id") or ""

    def _option_id(field_name: str, option_name: str) -> str:
        opts = (fields.get(field_name) or {}).get("options") or {}
        # Try exact match first, then case-insensitive
        if option_name in opts:
            return opts[option_name]
        for k, v in opts.items():
            if k.lower() == option_name.lower():
                return v
        return ""

    # Number fields
    for field_name, value in [
        ("Cost to date", values.cost_to_date),
        ("Turns", float(values.turns)),
        ("Attempts", float(values.attempts)),
    ]:
        fid = _field_id(field_name)
        if not fid:
            LOGGER.debug("No binding for field %r; skipping", field_name)
            continue
        try:
            _set_number(board_token, board_id, item_id, fid, value)
            LOGGER.info("Wrote %s=%s to item %s", field_name, value, item_id)
        except Exception as exc:
            LOGGER.warning("Failed to write %s: %s", field_name, exc)

    # Single-select fields
    for field_name, option_name in [
        ("Outcome", values.outcome),
        ("Clean exit", values.clean_exit),
    ]:
        if not option_name:
            continue
        fid = _field_id(field_name)
        if not fid:
            LOGGER.debug("No binding for field %r; skipping", field_name)
            continue
        oid = _option_id(field_name, option_name)
        if not oid:
            LOGGER.warning("No option id for %r / %r; skipping", field_name, option_name)
            continue
        try:
            _set_single_select(board_token, board_id, item_id, fid, oid)
            LOGGER.info("Wrote %s=%r to item %s", field_name, option_name, item_id)
        except Exception as exc:
            LOGGER.warning("Failed to write %s: %s", field_name, exc)


# ── Public entry points ───────────────────────────────────────────────────────

def project_provisional(
    board_token: str,
    issue_number: int,
    item_id: str,
    bindings_path: Path | None = None,
    target_repo: str | None = None,
) -> None:
    """Reduce JSONL records for issue_number and write provisional telemetry.

    Called from the runner main() after persist_record().  Best-effort:
    any exception is caught, logged, and swallowed so the run's exit code
    is never affected.

    Args:
        board_token:  Board App installation token.
        issue_number: The issue this run was triggered by.
        item_id:      The Projects v2 item node ID.
        bindings_path: Override path to field-bindings.json (for testing).
        target_repo:  owner/repo of the target repo (overrides TARGET_REPO env).
    """
    try:
        bindings = _load_bindings(bindings_path)
        board_id = _resolve_board_id(bindings)
        if not board_id:
            LOGGER.debug("board_id not set (BOARD_ID env / field-bindings.json); skipping projection")
            return
        if not item_id:
            LOGGER.debug("item_id empty; issue not on board, skipping projection")
            return
        if not bindings:
            LOGGER.debug("No bindings available; skipping projection")
            return

        repo = target_repo or os.environ.get("TARGET_REPO", "")
        records = _load_records_for_issue(issue_number, repo)
        values = reduce_runs(records)
        write_telemetry(board_token, board_id, item_id, values, bindings)
        LOGGER.info(
            "Projected provisional telemetry for issue #%s: cost=%.4f attempts=%d turns=%d",
            issue_number, values.cost_to_date, values.attempts, values.turns,
        )
    except Exception as exc:
        LOGGER.warning("project_provisional failed for issue #%s: %s", issue_number, exc)


def _parse_linked_issue(pr_body: str) -> int | None:
    """Extract the first linked issue number from a PR body.

    Matches: 'Closes #N', 'Fixes #N', 'Resolves #N' (case-insensitive).
    Returns the issue number as int, or None if not found.
    """
    if not pr_body:
        return None
    m = re.search(
        r"(?:closes|fixes|resolves)\s+#(\d+)",
        pr_body,
        re.IGNORECASE,
    )
    return int(m.group(1)) if m else None


def settle(
    board_token: str,
    pr_number: int,
    merged: bool,
    bindings_path: Path | None = None,
    target_repo: str | None = None,
) -> None:
    """Write the final Outcome field when a PR is closed.

    Resolves the linked issue from the PR body, finds its board item, and
    sets Outcome to 'Merged' or 'Closed unmerged'.  Best-effort throughout.

    Args:
        board_token:  Board App installation token.
        pr_number:    The closed PR number.
        merged:       True if the PR was merged, False if simply closed.
        bindings_path: Override path to field-bindings.json (for testing).
        target_repo:  owner/repo of the target repo (overrides TARGET_REPO env).
    """
    try:
        bindings = _load_bindings(bindings_path)
        board_id = _resolve_board_id(bindings)
        if not board_id:
            LOGGER.debug("board_id not set; skipping settlement")
            return

        if not bindings:
            LOGGER.debug("No bindings available; skipping settlement")
            return

        repo = target_repo or os.environ.get("TARGET_REPO", "")
        if not repo:
            LOGGER.warning("settle: TARGET_REPO not set; cannot fetch PR body")
            return

        # Fetch the PR body to resolve the linked issue number
        code_url = f"{API}/repos/{repo}/pulls/{pr_number}"
        req = urllib.request.Request(code_url)
        req.add_header("Authorization", f"Bearer {board_token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "ops-projector")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                pr_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            LOGGER.warning("settle: could not fetch PR #%s: %s", pr_number, exc)
            return

        pr_body = pr_data.get("body") or ""
        issue_number = _parse_linked_issue(pr_body)
        if not issue_number:
            LOGGER.debug("settle: no linked issue found in PR #%s body; no-op", pr_number)
            return

        # Find the board item for that issue using the Projects v2 GraphQL API.
        # Callers that have a `read_item_fields` helper can pass item_id directly
        # to write_telemetry; this path re-queries to stay self-contained.
        item_id = _find_item_id_for_issue(board_token, board_id, issue_number)
        if not item_id:
            LOGGER.debug("settle: issue #%s not on board; no-op", issue_number)
            return

        outcome_name = "Merged" if merged else "Closed unmerged"
        fields = bindings.get("fields", {})
        outcome_field = fields.get("Outcome") or {}
        field_id = outcome_field.get("id") or ""
        options = outcome_field.get("options") or {}
        option_id = options.get(outcome_name) or ""
        if not field_id or not option_id:
            LOGGER.warning(
                "settle: missing binding for Outcome/%r; field_id=%r option_id=%r",
                outcome_name, field_id, option_id,
            )
            return

        _set_single_select(board_token, board_id, item_id, field_id, option_id)
        LOGGER.info(
            "settle: set Outcome=%r on item %s (issue #%s, PR #%s merged=%s)",
            outcome_name, item_id, issue_number, pr_number, merged,
        )
    except Exception as exc:
        LOGGER.warning("settle failed for PR #%s: %s", pr_number, exc)


def _find_item_id_for_issue(token: str, board_id: str, issue_number: int) -> str:
    """Query the Projects v2 board for the item node ID linked to an issue.

    Returns the item node ID string, or empty string if not found.
    This is a lightweight GraphQL search; callers with a cached item_id
    should pass it directly to write_telemetry instead.
    """
    query = """
    query FindItem($boardId: ID!, $cursor: String) {
      node(id: $boardId) {
        ... on ProjectV2 {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content {
                ... on Issue { number }
              }
            }
          }
        }
      }
    }
    """
    cursor = None
    while True:
        variables: dict[str, Any] = {"boardId": board_id}
        if cursor:
            variables["cursor"] = cursor
        resp = _graphql(token, query, variables)
        nodes = (
            (resp.get("data") or {})
            .get("node", {})
            .get("items", {})
            .get("nodes", [])
        ) or []
        for node in nodes:
            content = node.get("content") or {}
            if content.get("number") == issue_number:
                return node.get("id") or ""
        page_info = (
            (resp.get("data") or {})
            .get("node", {})
            .get("items", {})
            .get("pageInfo", {})
        ) or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return ""


# ── CLI entry point for workflow automation ───────────────────────────────────

def _str_to_bool(value: str) -> bool:
    """Parse a command-line boolean flag."""
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value!r}")


def _pr_payload(event_path: str | None) -> tuple[int | None, bool]:
    """Extract pull_request number and merged flag from a GitHub event payload."""
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        return None, False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        LOGGER.warning("Could not read PR event payload from %s", path)
        return None, False
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    merged = bool(pr.get("merged", False))
    return (int(number) if number is not None else None), merged


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point used by the settlement workflow.

    Reads the closed pull request from ``GITHUB_EVENT_PATH`` by default, or
    accepts ``--pr`` and ``--merged`` overrides for ad-hoc / testing use.
    """
    parser = argparse.ArgumentParser(
        description="GitHub Projects v2 settlement runner."
    )
    parser.add_argument(
        "--pr",
        dest="pr_number",
        type=int,
        default=None,
        help="Closed PR number. Defaults to number from GITHUB_EVENT_PATH.",
    )
    parser.add_argument(
        "--merged",
        type=_str_to_bool,
        default=None,
        help="Whether the PR was merged (true/false). Defaults to event payload.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token. Defaults to the GITHUB_TOKEN environment variable.",
    )
    parser.add_argument(
        "--event-path",
        default=None,
        help="Path to a GitHub pull_request event payload JSON.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/repo of the target repository. Defaults to TARGET_REPO env.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Resolve board_id from env or field-bindings.json
    bindings = _load_bindings()
    board_id = _resolve_board_id(bindings)
    if not board_id:
        LOGGER.info("board_id is not configured (BOARD_ID env / field-bindings.json); skipping settlement.")
        return 0

    pr_number, merged = _pr_payload(args.event_path)
    if args.pr_number is not None:
        pr_number = args.pr_number
    if args.merged is not None:
        merged = args.merged

    if not pr_number:
        LOGGER.warning("No PR number available; skipping settlement.")
        return 0

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("BOARD_TOKEN")
    if not token:
        LOGGER.warning("No GitHub token available; skipping settlement.")
        return 0

    target_repo = args.repo or os.environ.get("TARGET_REPO", "")
    settle(token, pr_number, merged, target_repo=target_repo)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
