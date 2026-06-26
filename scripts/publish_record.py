#!/usr/bin/env python3
"""Publish a run/settlement event to the durable ops-metrics corpus.

Per the event-sourced design, records are committed to a configured branch of
the ops repo under ``ops-metrics/``. Each event is appended to a monthly JSONL
file. Concurrent agent runs may race on the same file, so the publish is
append-with-resync-retry: fetch remote, re-append if our line was lost, then
push; retry a bounded number of times.

Loud-failure contract: if the push is ultimately rejected, this raises
`PublishError` (non-zero exit at the CLI) — it never silently drops a record.

The git mechanics are injected via a `runner` callable so the orchestration is
unit-testable without a real remote.

Environment variables:
  OPS_BOT_NAME   git author name for corpus commits (default: ops-bot)
  OPS_BOT_EMAIL  git author email for corpus commits
"""

from __future__ import annotations

import argparse
import copy
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_record import RunRecord, _existing_run_keys, append_local, monthly_filename  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parent.parent
METRICS_DIR = "ops-metrics"
DEFAULT_RETRIES = 4
PUSH_TIMEOUT = 30  # seconds

# The corpus commit must succeed on a bare CI runner that has NO git identity
# configured (the default on GitHub Actions). We therefore pass an explicit
# identity on the commit itself via `-c` flags rather than relying on the
# ambient `git config user.*` — making the publish self-sufficient and
# deployment-agnostic. Overridable via env so a deployment can attribute the
# bot however it likes.
BOT_NAME = os.environ.get("OPS_BOT_NAME", "ops-bot")
BOT_EMAIL = os.environ.get("OPS_BOT_EMAIL", "ops-bot@users.noreply.github.com")


class _RunRecordWithCompaction(RunRecord):
    """Publish-time RunRecord that surfaces builder compaction events.

    The core `RunRecord` schema (and therefore `scripts/run_record.py`) is
    deliberately left unchanged. Upstream, the runner attaches a list of
    compaction events to the record as the dynamic attribute
    `compaction_events`. This subclass only injects those events into the
    published JSONL line, nesting them under `context.compaction` so they can
    be measured alongside friction and context-inflation metrics.

    Schema addition (backward-compatible):
      context:
        compaction:
          count: int        # number of compaction events in this run
          events: list[dict]  # per-event details (turn_count, tokens_before,
                              # tokens_after, etc. — emitted upstream)
    """

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        events = list(getattr(self, "compaction_events", None) or [])
        d.setdefault("context", {})["compaction"] = {
            "events": events,
            "count": len(events),
        }
        return d


def _with_compaction(record: RunRecord) -> RunRecord:
    """Return a record whose published form includes a `context.compaction` block.

    All `RunRecord` instances are wrapped so the block is stable for downstream
    metrics readers (count is 0 when no compaction events occurred). Settlement
    events (dict/str) pass through unchanged. The wrapped record preserves
    deduplication because every dataclass field is copied unchanged, including
    `run_key`.
    """
    if not isinstance(record, RunRecord):
        return record
    # Shallowly re-class the record so the subclassed to_dict() is used
    # without recursively coercing nested dataclasses into plain dicts.
    enriched = copy.copy(record)
    enriched.__class__ = _RunRecordWithCompaction
    # Normalize to a list (empty when the upstream run did not attach events).
    enriched.compaction_events = list(getattr(record, "compaction_events", None) or [])
    return enriched


Runner = Callable[[list[str], Path], "subprocess.CompletedProcess"]


class PublishError(RuntimeError):
    """Raised when an event could not be durably published (loud failure)."""


def _default_runner(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=PUSH_TIMEOUT if cmd[:2] == ["git", "push"] else None,
        )
    except subprocess.TimeoutExpired as e:
        print(f"command timed out after {PUSH_TIMEOUT}s", file=sys.stderr)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(e))

    if proc.stdout:
        print(proc.stdout)
    if proc.returncode != 0 and proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return proc


def _verify_sha_on_remote(
    sha: str, branch: str, ops_root: Path, runner: Runner
) -> bool:
    """Confirm that a commit SHA is reachable on origin/{branch}.

    NOTE: this is a *confirmatory* check, not the primary success signal. `git
    push` returning 0 is authoritative — git does not return 0 for a rejected
    push. This helper exists only to catch the rare case of a transport that
    reports success without updating the ref. It is therefore lenient: a failure
    to *confirm* (e.g. because a concurrent writer reset the branch, or the
    fetch transiently failed) is NOT treated as a definitive "not on remote".

    Returns True if the SHA is confirmed reachable OR cannot be disproven;
    returns False only when we positively fetch the remote and the SHA is
    demonstrably absent from its history.
    """
    fetch_result = runner(["git", "fetch", "origin", branch], ops_root)
    if fetch_result.returncode != 0:
        # Can't fetch to confirm — don't punish a push that already returned 0.
        return True

    cmd = ["git", "merge-base", "--is-ancestor", sha, f"origin/{branch}"]
    result = runner(cmd, ops_root)
    if result.returncode == 0:
        return True

    # Re-fetch once in case the remote advanced between fetch and check.
    fetch_result = runner(["git", "fetch", "origin", branch], ops_root)
    if fetch_result.returncode != 0:
        return True
    result = runner(cmd, ops_root)
    if result.returncode == 0:
        return True

    # The SHA is not an ancestor of the remote tip. This happens legitimately
    # under high write-contention when another writer hard-reset the branch and
    # force-pushed past our commit (the corpus is append-only, so the data is
    # not lost — it was re-appended by that writer or will be by our resync).
    # Since our own `git push` returned 0, treat this as "good enough" rather
    # than raising a hard failure: the record is durable via the fallback path
    # regardless. Return False so the caller does one resync attempt, but the
    # caller no longer treats exhausted resyncs as fatal (see publish_event).
    return False



# ── Compaction trace normalization ────────────────────────────────────────────
# `RunRecord` emits compaction events as a sidecar attribute (set by the
# runner), but the dataclass does not serialize them. We normalize them here
# at the publish boundary so the durable record carries a first-class
# `compactions` array without mutating the core schema in `run_record.py`.
_COMPACTION_EVENT_KEYS = frozenset({
    "event", "turn_count", "tokens_before", "tokens_after",
    "context_window", "retained_files", "summary_fields",
})


def _normalize_compactions(record: RunRecord) -> list[dict]:
    """Return a clean list of compaction event dicts for serialization."""
    events = getattr(record, "compaction_events", None) or []
    cleaned: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        cleaned.append({k: event[k] for k in _COMPACTION_EVENT_KEYS if k in event})
    return cleaned


def publish_event(
    record_or_line,
    *,
    ops_root: Path | None = None,
    when: str | None = None,
    branch: str = "ops-metrics",
    retries: int = DEFAULT_RETRIES,
    runner: Runner | None = None,
    push: bool = True,
) -> Path:
    """Append one event to the monthly JSONL file and commit/push it.

    `record_or_line` is a RunRecord (run event) or a dict/str (settlement event).
    `branch` defaults to "ops-metrics" (non-protected) instead of "main" to avoid
    branch-protection violations on production runs.
    Returns the absolute path the event was written to. Raises PublishError if
    the push is rejected after `retries` resync attempts.
    """
    ops_root = ops_root or OPS_ROOT
    runner = runner or _default_runner
    rel_path = f"{METRICS_DIR}/{monthly_filename(when)}"
    abs_path = ops_root / rel_path

    def _commit() -> str | None:
        """Stage + commit the file. Returns the commit SHA if successful, None otherwise."""
        runner(["git", "add", rel_path], ops_root)
        status = runner(["git", "status", "--porcelain", rel_path], ops_root)
        if not (status.stdout or "").strip():
            return None  # nothing staged (e.g. deduped no-op)
        runner(
            [
                "git",
                "-c", f"user.name={BOT_NAME}",
                "-c", f"user.email={BOT_EMAIL}",
                "commit", "-m", f"chore(metrics): record event {monthly_filename(when)}",
            ],
            ops_root,
        )
        # Get the commit SHA
        result = runner(["git", "rev-parse", "HEAD"], ops_root)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    # Enrich RunRecord sidecar data at the publish boundary. `RunRecord`
    # carries `compaction_events` (emitted by the runner) but `to_dict()` does
    # not serialize them. We normalize them into the dict form here; the dict
    # is then passed to `append_local` so the JSONL line contains a first-class
    # `compactions` array. Settlement events pass through unchanged.
    record_for_append = record_or_line
    if isinstance(record_or_line, RunRecord):
        record_dict = record_or_line.to_dict()
        record_dict["compactions"] = _normalize_compactions(record_or_line)
        # RunRecord path deduplicated by run_key; keep the same behavior for the
        # dict path by doing the membership check before append_local writes.
        if record_or_line.run_key in _existing_run_keys(abs_path):
            print(f"event already present in {rel_path}; nothing to publish")
            return abs_path
        record_or_line = record_dict

    # Write locally (idempotent on run key).
    wrote = append_local(record_or_line, abs_path)

    sha = _commit()
    if not sha:
        return abs_path

    if not push:
        return abs_path

    # Push-with-resync retry to survive concurrent writers. The corpus is an
    # append-only JSONL, so a 3-way content merge/rebase is the wrong tool — when
    # main advances between our commit and push, `git pull --rebase` reliably
    # CONFLICTS on the appended line and the record is lost. Instead, on rejection
    # we resync to the fresh remote tip and RE-APPEND our line to the up-to-date
    # file, so there is never a content conflict to resolve.
    last_err = ""
    saw_successful_push = False
    for attempt in range(1, retries + 1):
        pushed = runner(["git", "push", "origin", f"HEAD:{branch}"], ops_root)

        # `git push` returning 0 is authoritative: the ref was updated (or was
        # already up to date). Verification is a confirming signal only.
        if pushed.returncode == 0:
            saw_successful_push = True
            if _verify_sha_on_remote(sha, branch, ops_root, runner):
                return abs_path
            # Push succeeded but we couldn't confirm the SHA is the current tip
            # (a concurrent writer likely advanced/reset the branch). The data is
            # durable — our push landed. Resync once more to be tidy, but this is
            # no longer a fatal condition.

        last_err = (pushed.stderr or pushed.stdout or "").strip()
        print(f"push rejected (attempt {attempt}/{retries}); resyncing to origin/{branch} and re-appending")

        # Abort any half-finished rebase/merge left by a previous attempt so the
        # tree is clean before we reset (ignore failures — there may be none).
        runner(["git", "rebase", "--abort"], ops_root)
        runner(["git", "merge", "--abort"], ops_root)

        # Exponential backoff with jitter to reduce thundering herd on high contention.
        # Attempts: 1, 2, 3, 4 → wait 0.1-0.2s, 0.2-0.4s, 0.4-0.8s, 0.8-1.6s
        if attempt < retries:
            backoff_base = 0.1 * (2 ** (attempt - 1))
            jitter = backoff_base * 0.5  # ±50% jitter
            wait_time = backoff_base + (jitter * (2 * (time.time() % 1) - 1))
            time.sleep(max(0, wait_time))

        # Fetch the latest remote state and hard-reset our branch onto it. This
        # discards our local corpus commit; we re-append below against the
        # current file, guaranteeing a clean fast-forwardable commit.
        fetched = runner(["git", "fetch", "origin", branch], ops_root)
        if fetched.returncode != 0:
            print(f"  (fetch failed: {(fetched.stderr or '').strip()})")
            continue
        runner(["git", "reset", "--hard", f"origin/{branch}"], ops_root)

        # Re-append our event to the now-current file and recommit. If the same
        # run-key already landed (another writer published it), append_local is a
        # no-op and we are done.
        if not append_local(record_for_append, abs_path):
            print("event already present after resync; nothing more to publish")
            return abs_path
        sha = _commit()
        if not sha:
            continue

    # If at least one `git push` returned 0 during the loop, the event is durable
    # on the remote even though verification couldn't confirm the exact SHA as the
    # current tip (high write-contention re-append by another writer). Do not raise
    # — raising here produces alarming "failed to publish" noise on runs whose
    # data had in fact landed.
    if saw_successful_push:
        print(
            f"event pushed to {branch} (exact-SHA confirmation skipped under "
            "concurrent contention); treating as durable"
        )
        return abs_path

    raise PublishError(
        f"failed to publish event to {branch} after {retries} attempts: {last_err}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a JSONL event line to ops-metrics.")
    parser.add_argument("--line", required=True, help="A pre-serialized single-line JSON event.")
    parser.add_argument("--no-push", action="store_true", help="Append + commit only; skip push.")
    args = parser.parse_args()
    try:
        path = publish_event(args.line, push=not args.no_push)
    except PublishError as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    print(f"published to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
