#!/usr/bin/env python3
"""Structured run records for the agent observability layer.

This module is the source of truth for the *shape* of an agent run record and
the pure logic that derives observability signals from a run's trajectory. It is
deliberately I/O-free at its core (instrumentation + derivation); persistence
(`append_local`) and surfacing (`render_comment`) are thin helpers layered on
top in later units, but kept here so the schema and its renderers live together.

Design rules (from the v2 plan):
  - The runner is the source of truth (D1): it observes every turn, tool call,
    token count and exit. The agent cannot "forget" to report.
  - Records are event-sourced (D2): a run event is written at run-end; a separate
    settlement event is appended later. Records are never mutated in place.
  - No secrets ever enter the corpus (D9 / U-1 no-secrets rule): the trajectory
    stores each tool call's *arg shape* (the key names) plus a one-way hash of
    the arguments — never the raw argument values, and never tokens.
  - Token capture is exact and free (D5): read `usage` off every API response.

Two-phase lifecycle (D6): the runner writes a *provisional* record
(`outcome=provisional`); a later settlement reconciles it against the real
environment state (merged / CI / reviewer / revert).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

sys_path_modified = False
try:
    # Try importing cost_rates from the same directory
    from cost_rates import calculate_cost_usd
except ImportError:
    try:
        from .cost_rates import calculate_cost_usd
    except ImportError:
        pass
    # Fallback: will be set later if needed
    calculate_cost_usd = None
    sys_path_modified = True

SCHEMA_VERSION = 6


# ── Enums ─────────────────────────────────────────────────────────────────────

class CleanExit(str, enum.Enum):
    """How the agentic loop terminated — an *execution* (output) signal.

    This is what the runner knows at run-end. It says nothing about whether the
    work was good; that is the settlement's job (see Outcome).
    """

    CLEAN = "clean"                  # model stopped calling tools normally
    CRASHED = "crashed"              # an exception inside the loop
    MAX_TURNS = "max_turns"          # hit the hard turn ceiling
    INFRA_FAILURE = "infra_failure"  # token mint / missing key / SDK import / clone


class Outcome(str, enum.Enum):
    """The settled *environment* state — the outcome (not output) signal.

    `PROVISIONAL` is the run-end placeholder until a settlement event arrives.
    """

    PROVISIONAL = "provisional"
    MERGED = "merged"
    CLOSED_UNMERGED = "closed_unmerged"
    CI_FAILED = "ci_failed"
    REVERTED = "reverted"
    ABANDONED = "abandoned"


# ── Compaction events ───────────────────────────────────────────────────────────

@dataclass
class CompactionEvent:
    """One context-compaction operation recorded during a run.

    Emitted by the runner when compaction fires at a clean turn boundary.
    The event records the turn at which compaction occurred and the estimated
    token reduction achieved by replacing the conversation history with a
    deterministic summary.

    Fields:
        turn_count: turn number when compaction fired (1-based)
        tokens_before: estimated tokens in conversation before compaction
        tokens_after: estimated tokens in conversation after compaction
        context_window: provider/model context window used for the ratio check
        retained_files: paths of the N most recently accessed files re-attached
        summary_fields: names of the summary sections that were included
    """

    turn_count: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    context_window: int = 0
    retained_files: list[str] = field(default_factory=list)
    summary_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_count": self.turn_count,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "context_window": self.context_window,
            "retained_files": list(self.retained_files),
            "summary_fields": list(self.summary_fields),
        }


# ── Trajectory + friction ──────────────────────────────────────────────────────

def hash_args(args: dict[str, Any] | None) -> str:
    """One-way, stable hash of a tool call's arguments.

    Used for repeat/loop detection without persisting argument *values* (which
    may contain secrets, tokens, or large payloads). Two identical calls hash to
    the same value; the original arguments cannot be recovered from the hash.
    """
    if not args:
        return hashlib.sha256(b"").hexdigest()[:16]
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class TrajectoryEntry:
    """One tool call in the run's trajectory — payload-free.

    `arg_shape` is the sorted list of argument key names (the *shape*), and
    `arg_hash` is a one-way hash of the argument values. Neither leaks secrets.
    `status` is "ok" or "error" — enough for friction and a future grader.
    """

    tool: str
    arg_shape: list[str]
    arg_hash: str
    status: str  # "ok" | "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arg_shape": list(self.arg_shape),
            "arg_hash": self.arg_hash,
            "status": self.status,
        }


@dataclass
class ErrorDetails:
    """Structured error information when clean_exit.status is 'crashed'."""

    error_type: str = "unknown"  # encoding|auth|git|api_limit|timeout|unknown
    tool: str = ""               # last tool name that was running
    code: str = ""               # exit code or exception class name

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

@dataclass
class Friction:
    """Process-friction / error-recovery churn (H4) derived from the trajectory.

    A leading indicator and triage signal, captured at zero extra API cost.
    """

    tool_errors: int = 0
    retries: int = 0
    repeats: int = 0
    max_turns_proximity: float = 0.0  # turns / max_turns, clamped to [0, 1]
    tool_error_breakdown: list[dict[str, Any]] = field(default_factory=list)  # [{"tool": str, "count": int}]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def derive_friction(
    trajectory: list[TrajectoryEntry], turns: int, max_turns: int
) -> Friction:
    """Compute friction counts from an ordered trajectory.

    - tool_errors: calls that returned an "error" status.
    - retries:     an errored call immediately re-invoked (same name + arg hash)
                   on the very next call — the "try again right away" signature.
    - repeats:     a call whose (name, arg_hash) matches *any* earlier call in
                   the run — the "spinning / looping" signature. (A different
                   arg hash is a different target and is not a repeat.)
    - max_turns_proximity: how close the run came to the turn ceiling.
    - tool_error_breakdown: count of errors per distinct tool.
    """
    tool_errors = 0
    retries = 0
    repeats = 0
    seen: set[tuple[str, str]] = set()
    tool_error_counts: dict[str, int] = {}

    for i, entry in enumerate(trajectory):
        key = (entry.tool, entry.arg_hash)
        if entry.status == "error":
            tool_errors += 1
            tool_error_counts[entry.tool] = tool_error_counts.get(entry.tool, 0) + 1
        if key in seen:
            repeats += 1
        else:
            seen.add(key)
        # retry: previous call errored and this one repeats the same target.
        if i > 0:
            prev = trajectory[i - 1]
            if prev.status == "error" and (prev.tool, prev.arg_hash) == key:
                retries += 1

    proximity = (turns / max_turns) if max_turns > 0 else 0.0
    proximity = max(0.0, min(1.0, proximity))

    # Build tool_error_breakdown sorted by tool name
    tool_error_breakdown = [
        {"tool": tool, "count": count}
        for tool, count in sorted(tool_error_counts.items())
    ]

    return Friction(
        tool_errors=tool_errors,
        retries=retries,
        repeats=repeats,
        max_turns_proximity=round(proximity, 4),
        tool_error_breakdown=tool_error_breakdown,
    )


# ── The run record ──────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunRecord:
    """A single agent run (one invocation) — the provisional event.

    Identity is keyed on `(repo, role, kind, number, run_id, attempt)` (D4): a
    PR may have many runs (initial build, rework, reviewer passes) and each is
    its own record, which is what makes attempts-to-land (H2) computable.

    `identity.attempt` (the attempt_number) is a complexity-relevant signal:
    a first attempt that lands cleanly signals a well-scoped task, while
    repeated attempts (rework after review) indicate scope or specification
    friction. It is surfaced as a named field in `to_dict()['identity']` so
    analytics can correlate attempts-to-land with cost and complexity.
    """

    # ── identity ──
    repo: str
    role: str
    kind: str            # "issue" | "pull_request"
    number: int
    agent_identity: str = ""
    run_id: str = ""
    attempt: int = 1
    github_actions_run_url: str = ""

    # ── lifecycle ──
    started_at: str = field(default_factory=_utc_now_iso)
    ended_at: str = ""
    duration_seconds: float = 0.0

    # ── execution ──
    turns: int = 0
    tool_calls: int = 0
    max_turns_hit: bool = False
    trajectory: list[TrajectoryEntry] = field(default_factory=list)

    # ── compaction events ──
    compaction_events: list[CompactionEvent] = field(default_factory=list)

    # ── cost ──
    input_tokens: int = 0
    output_tokens: int = 0
    per_turn_tokens: list[dict[str, int]] = field(default_factory=list)

    # ── derived friction (H4) ──
    friction: Friction = field(default_factory=Friction)

    # ── clean-exit status (execution / output signal) ──
    clean_exit: CleanExit = CleanExit.CLEAN
    clean_exit_detail: str = ""
    clean_exit_error: ErrorDetails = field(default_factory=ErrorDetails)

    # ── linkage + outcome placeholder ──
    pr_number: int = 0       # the PR this run produced or acted on (0 if none)
    issue_number: int = 0    # the issue this run was triggered from
    previous_run_id: str = ""  # run_id of the previous attempt (for re-tries)
    outcome: Outcome = Outcome.PROVISIONAL

    # ── context enrichment (optional for builder runs) ──
    diff_lines_added: int = 0
    diff_lines_removed: int = 0
    files_changed_count: int = 0  # number of files touched in this run
    issue_labels: list[str] = field(default_factory=list)
    model_version: str = "unknown"  # LLM model version (provider:model format)
    # Populated by the runner from the resolved backend; default to empty
    # string if the model cannot be determined before the record is written.
    model_provider: str = ""
    model_name: str = ""
    projects_board_item_id: str = ""
    projects_board_fields: dict = field(default_factory=dict)

    # ── derived context signal (context inflation) ──
    # ratio of the final turn's input tokens to the first turn's input
    # tokens; 0.0 when there are fewer than two turns or the first turn
    # had no input tokens (guard against divide-by-zero). Computed in
    # finalize() so it reflects the complete per-turn trajectory.
    context_inflation_ratio: float = 0.0

    # ── pre-execution complexity signals (parsed from issue body) ──
    # Number of file bullets listed in the issue's capability grant section
    # and the count of declared blocking dependencies.
    # Populated once from the issue body at run start; intrinsic to the issue.
    capability_grant_file_count: int = 0
    declared_dependency_count: int = 0

    # ── helpers used by the runner during a live loop ──

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_tool_call(self, entry: TrajectoryEntry) -> None:
        self.trajectory.append(entry)
        self.tool_calls += 1

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.per_turn_tokens.append(
            {"input": int(input_tokens or 0), "output": int(output_tokens or 0)}
        )

    def set_clean_exit(
        self, status: CleanExit, detail: str = "", max_turns: int | None = None
    ) -> None:
        self.clean_exit = status
        self.clean_exit_detail = detail
        if status == CleanExit.MAX_TURNS:
            self.max_turns_hit = True
        if max_turns is not None:
            # refresh proximity now that we know the ceiling
            self.friction.max_turns_proximity = round(
                max(0.0, min(1.0, (self.turns / max_turns) if max_turns > 0 else 0.0)), 4
            )

    def finalize(self, max_turns: int) -> None:
        """Stamp end time/duration and (re)derive friction from the trajectory.

        Called once when the loop exits, before persistence.
        """
        if not self.ended_at:
            self.ended_at = _utc_now_iso()
        if self.started_at and self.ended_at and not self.duration_seconds:
            try:
                fmt = "%Y-%m-%dT%H:%M:%SZ"
                start = datetime.strptime(self.started_at, fmt)
                end = datetime.strptime(self.ended_at, fmt)
                self.duration_seconds = max(0.0, (end - start).total_seconds())
            except ValueError:
                self.duration_seconds = 0.0
        self.friction = derive_friction(self.trajectory, self.turns, max_turns)
        if self.clean_exit == CleanExit.MAX_TURNS:
            self.max_turns_hit = True
        self._compute_context_inflation_ratio()

    def _compute_context_inflation_ratio(self) -> None:
        """Derive `context_inflation_ratio` from the per-turn token trajectory.

        Ratio = final_turn_input_tokens / first_turn_input_tokens.
        Yields 0.0 safely when there are fewer than two turns or the first
        turn carried zero input tokens (guard against divide-by-zero).
        """
        turns = self.per_turn_tokens
        if len(turns) < 2:
            self.context_inflation_ratio = 0.0
            return
        first = int(turns[0].get("input", 0) or 0)
        final = int(turns[-1].get("input", 0) or 0)
        if first <= 0:
            self.context_inflation_ratio = 0.0
            return
        self.context_inflation_ratio = round(final / first, 4)

    @property
    def run_key(self) -> str:
        """The dedupe / settlement key: (repo, role, kind, number, run_id, attempt)."""
        return "|".join(
            str(x) for x in (self.repo, self.role, self.kind, self.number, self.run_id, self.attempt)
        )

    def to_dict(self) -> dict[str, Any]:
        # Calculate USD costs from token counts, preferring per-model rates.
        cost_fn = calculate_cost_usd
        if cost_fn is None:
            # Fallback in case import failed; lazy-load
            try:
                from cost_rates import calculate_cost_usd_for_model as _cfn
                cost_fn = _cfn
            except ImportError:
                # Provide zero costs if cost_rates cannot be imported
                def _zero_cost(provider, model, i, o):
                    return {"input_cost_usd": 0.0, "output_cost_usd": 0.0, "total_cost_usd": 0.0}
                cost_fn = _zero_cost
        elif getattr(cost_fn, "__name__", None) == "calculate_cost_usd":
            # Prefer the model-aware variant when only the base import resolved.
            try:
                from cost_rates import calculate_cost_usd_for_model as _cfn
                cost_fn = _cfn
            except ImportError:
                pass

        cost_breakdown = cost_fn(
            self.model_provider,
            self.model_name,
            self.input_tokens,
            self.output_tokens,
        )

        cost_block = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_cost_usd": cost_breakdown["input_cost_usd"],
            "output_cost_usd": cost_breakdown["output_cost_usd"],
            "total_cost_usd": cost_breakdown["total_cost_usd"],
            "modeled_cost_usd": cost_breakdown["total_cost_usd"],
            "per_turn": list(self.per_turn_tokens),
        }
        # Only expose model-specific rates when a provider+model were resolved.
        if self.model_provider and self.model_name:
            cost_block["model_input_rate_usd"] = cost_breakdown["model_input_rate_usd"]
            cost_block["model_output_rate_usd"] = cost_breakdown["model_output_rate_usd"]

        return {
            "schema_version": SCHEMA_VERSION,
            "event": "run",
            "run_key": self.run_key,
            "identity": {
                "repo": self.repo,
                "role": self.role,
                "kind": self.kind,
                "number": self.number,
                "agent_identity": self.agent_identity,
                "run_id": self.run_id,
                "attempt": self.attempt,
                "github_actions_run_url": self.github_actions_run_url,
                "model_provider": self.model_provider,
                "model_name": self.model_name,
            },
            "lifecycle": {
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "duration_seconds": self.duration_seconds,
            },
            "execution": {
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "max_turns_hit": self.max_turns_hit,
                "compaction": {
                    "count": len(self.compaction_events),
                    "events": [
                        e.to_dict() if hasattr(e, "to_dict") else dict(e)
                        for e in self.compaction_events
                    ],
                },
            },
            "trajectory": [e.to_dict() for e in self.trajectory],
            "cost": cost_block,
            "friction": self.friction.to_dict(),
            "context": {
                "diff_lines_added": self.diff_lines_added,
                "diff_lines_removed": self.diff_lines_removed,
                "files_changed_count": self.files_changed_count,
                "issue_labels": self.issue_labels,
                "model_version": self.model_version,
                "context_inflation_ratio": self.context_inflation_ratio,
                "capability_grant_file_count": self.capability_grant_file_count,
                "declared_dependency_count": self.declared_dependency_count,
                "projects_board_item_id": self.projects_board_item_id,
                "projects_board_fields": self.projects_board_fields,
            },
            "clean_exit": {
                "status": self.clean_exit.value,
                "detail": self.clean_exit_detail,
                "error": self.clean_exit_error.to_dict() if self.clean_exit == CleanExit.CRASHED else None,
            },
            "linkage": {
                "pr_number": self.pr_number,
                "issue_number": self.issue_number,
                "previous_run_id": self.previous_run_id,
            },
            "outcome": self.outcome.value,
        }


def to_json(record: RunRecord) -> str:
    """Serialize a record to a single JSON line (no embedded newlines).

    Compaction events may be plain dicts (legacy runner events) or
    `CompactionEvent` dataclass instances. Both are normalized to JSON-serializable
    dicts before serializing so downstream consumers always see the same shape.
    """
    payload = record.to_dict()
    events = []
    for ev in getattr(record, "compaction_events", None) or []:
        if isinstance(ev, dict):
            events.append(ev)
        else:
            events.append(ev.to_dict())
    payload["execution"]["compaction"] = {
        "count": len(events),
        "events": events,
    }
    return json.dumps(payload, separators=(",", ":"), default=str)


# ── Persistence (U-2) ───────────────────────────────────────────────────────────

def monthly_filename(when: str | None = None) -> str:
    """The dated JSONL filename a record belongs in: runs-YYYY-MM.jsonl.

    Records are bucketed by month so the corpus stays greppable and diffs stay
    small. `when` is an ISO timestamp; defaults to now.
    """
    ts = when or _utc_now_iso()
    yyyymm = ts[:7] if len(ts) >= 7 else datetime.now(timezone.utc).strftime("%Y-%m")
    return f"runs-{yyyymm}.jsonl"


def _existing_run_keys(path: Path) -> set[str]:
    """Read the run_keys already present in a JSONL file (for idempotency)."""
    keys: set[str] = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = obj.get("run_key")
        if obj.get("event") == "run" and key:
            keys.add(key)
    return keys


def append_local(record_or_line, path: str | Path) -> bool:
    """Append one event line to a JSONL file, creating parents as needed.

    Idempotent on the run key (D2/U-2): re-appending the same run event is a
    no-op (returns False). Accepts either a RunRecord or a pre-serialized line
    (so settlement events, which are dicts, can reuse this path).

    Returns True if a line was written, False if it was a deduped no-op.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(record_or_line, RunRecord):
        line = to_json(record_or_line)
        run_key = record_or_line.run_key
        # Only run events dedupe on run_key; settlement events are append-always.
        if run_key in _existing_run_keys(path):
            return False
    else:
        line = record_or_line if isinstance(record_or_line, str) else json.dumps(
            record_or_line, separators=(",", ":"), default=str
        )

    assert "\n" not in line, "a JSONL event must be a single line"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return True


# ── Surfacing (U-6) ─────────────────────────────────────────────────────────────

_RUN_RECEIPT_ANCHOR = "<!-- agent:run-receipt -->"
_SETTLEMENT_ANCHOR = "<!-- agent:run-settlement -->"


def build_settlement(
    run_key: str,
    outcome: Outcome,
    *,
    ci_result: str = "",
    reviewer_verdict: str = "none",
    reverted_at: str = "",
    reverted_by: str = "",
    settled_at: str | None = None,
    pr_number: int = 0,
    pipeline_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Build a settlement event (D6) keyed to a run.

    Settlement is a *new* event appended to the corpus — it never mutates the
    original run line (D2 append-only / event-sourcing). The metrics reader folds
    `run + latest settlement` by run key. `pipeline_cost_usd` is the sum of all
    agent run costs for the PR if available.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "settlement",
        "run_key": run_key,
        "settled_at": settled_at or _utc_now_iso(),
        "outcome": outcome.value if isinstance(outcome, Outcome) else str(outcome),
        "ci_result": ci_result,
        "reviewer_verdict": reviewer_verdict,
        "reverted_at": reverted_at,
        "reverted_by": reverted_by,
        "pr_number": pr_number,
        "pipeline_cost_usd": round(pipeline_cost_usd, 6) if pipeline_cost_usd else 0.0,
    }


def settlement_to_json(event: dict[str, Any]) -> str:
    return json.dumps(event, separators=(",", ":"), default=str)


def render_settlement(event: dict[str, Any]) -> str:
    """Render a settlement update comment for the issue/PR."""
    outcome = event.get("outcome", "provisional")
    emoji = {
        "merged": "✅", "closed_unmerged": "⬜", "ci_failed": "❌",
        "reverted": "↩️", "abandoned": "🚫",
    }.get(outcome, "•")
    lines = [
        _SETTLEMENT_ANCHOR,
        "### Agent run settled",
        "",
        f"- {emoji} outcome: **{outcome}**",
    ]
    if event.get("ci_result"):
        lines.append(f"- CI: `{event['ci_result']}`")
    if event.get("reviewer_verdict") and event["reviewer_verdict"] != "none":
        lines.append(f"- reviewer verdict: `{event['reviewer_verdict']}`")
    if event.get("reverted_at"):
        who = event.get("reverted_by") or "unknown"
        lines.append(f"- reverted at {event['reverted_at']} by {who}")
    return "\n".join(lines)


def render_comment(record: RunRecord) -> str:
    """Render the enriched, runner-authored traceability block for an issue/PR.

    Contains the clean-exit status, execution counts, friction, tokens, duration,
    and a run link when available — and never any secrets (the record carries none).
    """
    rid = record.github_actions_run_url
    run_link = f"[View run]({rid})" if rid else "_(no Actions URL)_"
    fr = record.friction
    failed = record.clean_exit != CleanExit.CLEAN
    headline = "❌ run did not exit cleanly" if failed else "✅ run exited cleanly"

    lines = [
        _RUN_RECEIPT_ANCHOR,
        "### Agent run record (provisional)",
        "",
        f"- **{headline}** — clean-exit: `{record.clean_exit.value}`",
    ]
    if record.clean_exit_detail:
        lines.append(f"- detail: {record.clean_exit_detail[:300]}")
    lines += [
        f"- role: `{record.role}` · kind: `{record.kind}` · #{record.number}"
        + (f" · PR #{record.pr_number}" if record.pr_number else ""),
        f"- turns: **{record.turns}** · tool calls: **{record.tool_calls}**"
        + (f" · max-turns hit" if record.max_turns_hit else ""),
        f"- friction — errors: {fr.tool_errors} · retries: {fr.retries} · repeats: {fr.repeats}",
        f"- tokens — in: {record.input_tokens} · out: {record.output_tokens} · "
        f"total: **{record.total_tokens}**",
    ]

    # Add cost line if there are any tokens
    if record.total_tokens > 0:
        cost_dict = {}
        try:
            from cost_rates import calculate_cost_usd
            cost_dict = calculate_cost_usd(record.input_tokens, record.output_tokens)
        except ImportError:
            pass

        total_cost = cost_dict.get("total_cost_usd", 0.0)
        lines.append(
            f"- cost — ${total_cost:.6f} ({record.input_tokens} in · {record.output_tokens} out)"
        )

    lines += [
        f"- duration: {record.duration_seconds:.0f}s · {run_link}",
        "",
        "_Outcome is `provisional` until the PR is settled (merge / CI / review)._",
    ]
    return "\n".join(lines)
