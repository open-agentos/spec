#!/usr/bin/env python3
"""Delivery helper: scan approved promote-ready PRs and run their delivery pipeline.

Implements a delivery policy for the ops repo: after the reviewer applies
`status:approved` to a PR, a human or automated dispatch can invoke this
script to promote approved changes.

In the current phase the "delivery pipeline" is a placeholder that prints a
human-readable confirmation and exits successfully. It is designed to be wired
to real deployment tooling later without changing the CLI contract.

Environment variables:
  TARGET_REPO   owner/repo to scan for approved PRs (required if --repo omitted)
  GITHUB_TOKEN  GitHub personal access token or installation token
  LOG_LEVEL     Python logging level (default: INFO)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
logger = logging.getLogger("promote_ready")


def gh_api(token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list]:
    """Make a GitHub API request and return (status_code, parsed_body)."""
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "ops-promote-ready")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            return exc.code, (json.loads(body) if body else {"message": body})
        except json.JSONDecodeError:
            return exc.code, {"message": body}


def list_approved_prs(token: str, owner_repo: str) -> list[dict]:
    """Return all open PRs in the repo having the `status:approved` label."""
    approved: list[dict] = []
    page = 1
    while True:
        code, body = gh_api(
            token, "GET",
            f"/repos/{owner_repo}/pulls?state=open&per_page=100&page={page}"
        )
        if code != 200:
            raise RuntimeError(f"GitHub API error {code}: {body}")
        prs = body if isinstance(body, list) else []
        for pr in prs:
            labels = [label.get("name", "") for label in pr.get("labels", [])]
            if "status:approved" in labels:
                approved.append(pr)
        if len(prs) < 100:
            break
        page += 1
    return approved


def post_promote_comment(token: str, owner_repo: str, pr_number: int, dry_run: bool) -> None:
    """Leave a run receipt on the PR so humans can see promotion happened."""
    status = "🟢 promoted" if not dry_run else "🔵 dry-run"
    body = (
        f"<!-- agent:promote-ready -->\n"
        f"**Promote Ready Delivery** — {status}\n\n"
        f"Approved PR #{pr_number} has passed through the promote-ready gate.\n"
        f"Delivery pipeline status: **success** (placeholder stage).\n"
    )
    code, resp = gh_api(
        token, "POST",
        f"/repos/{owner_repo}/issues/{pr_number}/comments",
        {"body": body},
    )
    if code != 201:
        logger.warning("Failed to post promote comment on PR %s: %s %s", pr_number, code, resp)
    else:
        logger.info("Posted promote comment on PR %s", pr_number)


def extract_closing_issue_number(pr: dict) -> int | None:
    """Parse PR body for a `Closes #N` closing reference."""
    if not pr or not pr.get("body"):
        return None
    match = re.search(r"[Cc]los(?:e|es|ed|ing)\s*#(\d+)", pr.get("body", ""))
    return int(match.group(1)) if match else None


def parse_wave_from_issue_body(body: str | None) -> int | None:
    """Extract the wave number from the `Wave:` line in an issue body."""
    if not body:
        return None
    match = re.search(r"^\s*Wave:\s*(\d+)", body, re.MULTILINE | re.IGNORECASE)
    return int(match.group(1)) if match else None


def fetch_sub_issues(token: str, owner_repo: str, issue_number: int) -> list[dict]:
    """Return child issues for a parent issue using GitHub's sub-issues preview API."""
    code, body = gh_api(
        token, "GET",
        f"/repos/{owner_repo}/issues/{issue_number}/sub_issues",
    )
    if code != 200:
        logger.warning("Could not fetch sub-issues for #%s: %s %s", issue_number, code, body)
        return []
    return body if isinstance(body, list) else []


def feature_issue_for_child(token: str, owner_repo: str, child_issue_number: int) -> dict | None:
    """Return the parent issue of a child issue, if any."""
    code, body = gh_api(
        token, "GET",
        f"/repos/{owner_repo}/issues/{child_issue_number}/parent",
    )
    if code != 200:
        logger.debug("No parent issue found for #%s: %s", child_issue_number, code)
        return None
    return body if isinstance(body, dict) else None


def _wave_complete_anchor(wave: int) -> str:
    return f"<!-- agent:wave-complete:{wave} -->"


def wave_complete_comment_already_posted(token: str, owner_repo: str, feature_number: int, wave: int) -> bool:
    """Check if the wave-complete signal for this wave already exists."""
    page = 1
    anchor = _wave_complete_anchor(wave)
    while True:
        code, body = gh_api(
            token, "GET",
            f"/repos/{owner_repo}/issues/{feature_number}/comments?per_page=100&page={page}",
        )
        if code != 200:
            logger.warning("Could not fetch comments for #%s: %s", feature_number, code)
            return False
        comments = body if isinstance(body, list) else []
        for comment in comments:
            if anchor in (comment.get("body") or ""):
                return True
        if len(comments) < 100:
            break
        page += 1
    return False


def post_wave_complete_comment(token: str, owner_repo: str, feature_number: int, wave: int, dry_run: bool) -> bool:
    """Post an idempotent wave-completion comment on the parent issue."""
    if wave_complete_comment_already_posted(token, owner_repo, feature_number, wave):
        logger.info("Wave %s completion comment already posted on Feature #%s; skipping", wave, feature_number)
        return False

    next_wave = wave + 1
    body = (
        f"{_wave_complete_anchor(wave)}\n"
        f"**Wave {wave} complete** — all wave-{wave} children are now closed.\n\n"
        f"When you are ready, trigger wave {next_wave} planning."
    )
    if dry_run:
        logger.info("[dry-run] Would post wave-complete comment on Feature #%s", feature_number)
        return True

    code, resp = gh_api(
        token, "POST",
        f"/repos/{owner_repo}/issues/{feature_number}/comments",
        {"body": body},
    )
    if code != 201:
        logger.warning("Failed to post wave-complete comment on Feature #%s: %s %s", feature_number, code, resp)
        return False
    logger.info("Posted wave-complete comment for wave %s on Feature #%s", wave, feature_number)
    return True


def evaluate_wave_completion(token: str, owner_repo: str, child_issue_number: int, dry_run: bool) -> bool:
    """Check if the child's wave is fully closed and, if so, signal the parent."""
    logger.info("Evaluating wave completion after promoting child issue #%s", child_issue_number)

    # Load child issue to determine its wave.
    code, child = gh_api(token, "GET", f"/repos/{owner_repo}/issues/{child_issue_number}")
    if code != 200:
        logger.warning("Could not fetch child issue #%s: %s %s", child_issue_number, code, child)
        return False
    wave = parse_wave_from_issue_body(child.get("body", ""))
    if wave is None:
        logger.info("Child issue #%s has no Wave field; skipping wave-completion check", child_issue_number)
        return False

    feature = feature_issue_for_child(token, owner_repo, child_issue_number)
    if not feature:
        logger.info("Child issue #%s has no parent; skipping wave-completion check", child_issue_number)
        return False
    feature_number = feature["number"]

    siblings = fetch_sub_issues(token, owner_repo, feature_number)
    if not siblings:
        logger.info("Feature #%s has no sub-issues; skipping wave-completion check", feature_number)
        return False

    wave_siblings = [
        s for s in siblings
        if parse_wave_from_issue_body(s.get("body", "")) == wave
    ]
    if not wave_siblings:
        logger.info("Feature #%s has no wave-%s children; skipping wave-completion check", feature_number, wave)
        return False

    all_closed = all(s.get("state") == "closed" for s in wave_siblings)
    if not all_closed:
        logger.info("Wave %s children of Feature #%s are not all closed yet", wave, feature_number)
        return False

    next_wave_exists = any(
        parse_wave_from_issue_body(s.get("body", "")) == wave + 1
        for s in siblings
    )
    if next_wave_exists:
        logger.info("Wave %s already exists for Feature #%s; no comment needed", wave + 1, feature_number)
        return False

    return post_wave_complete_comment(token, owner_repo, feature_number, wave, dry_run)


def deliver(pr: dict, dry_run: bool) -> bool:
    """Run the delivery pipeline for an approved PR.

    Placeholder implementation: prints a confirmation message and returns True.
    Wire this to real deployment tooling without changing the CLI contract.
    """
    logger.info("%s delivery for PR #%s: %s", "[dry-run]" if dry_run else "Running", pr["number"], pr["title"])
    print(f"Promoting approved PR #{pr['number']}: {pr['title']}")
    print("  delivery pipeline: placeholder (success)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan approved promote-ready PRs and run their delivery pipeline."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("TARGET_REPO"),
        help="owner/repo to scan (default: TARGET_REPO env)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be promoted without acting")
    parser.add_argument("--pr", type=int, default=0, help="Promote a single PR number instead of scanning")
    parser.add_argument("--skip-comment", action="store_true", help="Do not post a comment on promoted PRs")
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (default: GITHUB_TOKEN env)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

    if not args.repo:
        parser.error("--repo or TARGET_REPO env is required")

    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        parser.error("--token or GITHUB_TOKEN env is required")

    if args.pr:
        code, pr = gh_api(token, "GET", f"/repos/{args.repo}/pulls/{args.pr}")
        if code != 200:
            logger.error("Could not fetch PR %s: %s %s", args.pr, code, pr)
            return 1
        labels = [label.get("name", "") for label in pr.get("labels", [])]
        if "status:approved" not in labels:
            logger.error("PR %s does not have status:approved label; cannot promote", args.pr)
            return 1
        prs = [pr]
    else:
        prs = list_approved_prs(token, args.repo)

    if not prs:
        logger.info("No approved promote-ready PRs found in %s", args.repo)
        return 0

    failed = False
    for pr in prs:
        try:
            ok = deliver(pr, args.dry_run)
            if ok and not args.dry_run and not args.skip_comment:
                post_promote_comment(token, args.repo, pr["number"], args.dry_run)
            elif args.dry_run:
                post_promote_comment(token, args.repo, pr["number"], args.dry_run)

            closing_issue = extract_closing_issue_number(pr)
            if ok and closing_issue is not None:
                evaluate_wave_completion(token, args.repo, closing_issue, args.dry_run)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Delivery failed for PR %s: %s", pr["number"], exc)
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
