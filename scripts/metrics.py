#!/usr/bin/env python3
"""Aggregate the observability corpus into the health metric set.

Reads run + settlement events from the ops-metrics JSONL corpus, folds them by
run key (run + *latest* settlement), and computes the deliberately small health
quartet plus the cost line:

  H1  Task success rate (merge rate)         — outcome metric, the headline KPI
  H2  Attempts-to-land (reliability)         — distribution, the Pass^k analog
  H3  Clean-exit rate (execution / infra)    — output metric, demoted from KPI
  H4  Process friction (error-recovery)      — leading indicator + triage queue
  Cost: tokens per merged PR, per role       — budget line, not a health signal

The philosophy: never let an *output* metric (H3) masquerade as an *outcome*
metric (H1). A high H3 with a low H1 is the single most important failure
signature — agents run fine but produce bad work.

This module is the metrics reader; it is also the natural CLI entrypoint for
the operator summary report (--triage view included).

Environment variables:
  METRICS_DIR   Override the directory containing runs-*.jsonl files.
                Defaults to <repo-root>/ops-metrics.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
import os

OPS_ROOT = Path(__file__).resolve().parent.parent
METRICS_DIR = Path(os.environ.get("METRICS_DIR", "")) or (OPS_ROOT / "ops-metrics")

# A run is counted as "elevated friction" when its combined churn crosses this.
FRICTION_THRESHOLD = 2  # tool_errors + retries + repeats


@dataclass
class FoldedRun:
    """A run event folded with its latest settlement (the effective record)."""

    run_key: str
    repo: str = ""
    role: str = ""
    pr_number: int = 0
    issue_number: int = 0
    clean_exit: str = "clean"
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    friction: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    outcome: str = "provisional"
    ci_result: str = ""
    reviewer_verdict: str = "none"
    pipeline_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def friction_score(self) -> int:
        f = self.friction or {}
        return int(f.get("tool_errors", 0)) + int(f.get("retries", 0)) + int(f.get("repeats", 0))

    @property
    def elevated_friction(self) -> bool:
        return self.friction_score >= FRICTION_THRESHOLD


# ── Loading + folding ────────────────────────────────────────────────────────────

def load_events(paths: Iterable[Path]) -> list[dict]:
    events: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def corpus_files(metrics_dir: Path | None = None) -> list[Path]:
    metrics_dir = metrics_dir or METRICS_DIR
    if not metrics_dir.exists():
        return []
    return sorted(metrics_dir.glob("runs-*.jsonl"))


def _in_window(ts: str, start: str | None, end: str | None) -> bool:
    if start and ts and ts < start:
        return False
    if end and ts and ts > end:
        return False
    return True


def fold(events: list[dict], *, start: str | None = None, end: str | None = None) -> list[FoldedRun]:
    """Fold run + latest settlement by run key.

    Window filtering is applied on the *run* event's started_at, so a run is
    included or excluded as a unit regardless of when its settlement arrived.
    """
    runs: dict[str, FoldedRun] = {}
    settlements: dict[str, dict] = {}

    for ev in events:
        kind = ev.get("event")
        key = ev.get("run_key", "")
        if not key:
            continue
        if kind == "run":
            ident = ev.get("identity", {})
            linkage = ev.get("linkage", {})
            ts = ev.get("lifecycle", {}).get("started_at", "")
            if not _in_window(ts, start, end):
                continue
            cost_data = ev.get("cost", {})
            runs[key] = FoldedRun(
                run_key=key,
                repo=ident.get("repo", ""),
                role=ident.get("role", ""),
                pr_number=int(linkage.get("pr_number", 0) or 0),
                issue_number=int(linkage.get("issue_number", 0) or 0),
                clean_exit=ev.get("clean_exit", {}).get("status", "clean"),
                turns=ev.get("execution", {}).get("turns", 0),
                tool_calls=ev.get("execution", {}).get("tool_calls", 0),
                input_tokens=cost_data.get("input_tokens", 0),
                output_tokens=cost_data.get("output_tokens", 0),
                input_cost_usd=float(cost_data.get("input_cost_usd", 0) or 0),
                output_cost_usd=float(cost_data.get("output_cost_usd", 0) or 0),
                total_cost_usd=float(cost_data.get("total_cost_usd", 0) or 0),
                friction=ev.get("friction", {}),
                started_at=ts,
                outcome=ev.get("outcome", "provisional"),
            )
        elif kind == "settlement":
            prev = settlements.get(key)
            # keep the *latest* settlement by settled_at (revert flips success).
            if prev is None or ev.get("settled_at", "") >= prev.get("settled_at", ""):
                settlements[key] = ev

    for key, ev in settlements.items():
        run = runs.get(key)
        if run is None:
            continue  # settlement for an out-of-window run; ignore
        run.outcome = ev.get("outcome", run.outcome)
        run.ci_result = ev.get("ci_result", "")
        run.reviewer_verdict = ev.get("reviewer_verdict", "none")
        run.pipeline_cost_usd = float(ev.get("pipeline_cost_usd", 0) or 0)
        if not run.pr_number and ev.get("pr_number"):
            run.pr_number = int(ev["pr_number"])

    return list(runs.values())


# ── The metrics ────────────────────────────────────────────────────────────────

def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def h1_merge_rate(folded: list[FoldedRun]) -> dict[str, Any]:
    """Of PRs opened by agents, the fraction merged (and not reverted)."""
    pr_outcomes: dict[int, str] = {}
    for r in folded:
        if not r.pr_number:
            continue
        # A PR's outcome is the strongest/latest across its runs: prefer a
        # terminal outcome over provisional; reverted beats merged.
        cur = pr_outcomes.get(r.pr_number, "provisional")
        pr_outcomes[r.pr_number] = _stronger_outcome(cur, r.outcome)

    settled = {pr: o for pr, o in pr_outcomes.items() if o != "provisional"}
    merged = sum(1 for o in settled.values() if o == "merged")
    total = len(settled)
    return {
        "merge_rate_pct": _pct(merged, total),
        "merged_prs": merged,
        "settled_prs": total,
        "provisional_prs": len(pr_outcomes) - total,
        "by_pr": pr_outcomes,
    }


_OUTCOME_RANK = {
    "provisional": 0, "abandoned": 1, "closed_unmerged": 2,
    "ci_failed": 3, "merged": 4, "reverted": 5,
}


def _stronger_outcome(a: str, b: str) -> str:
    return a if _OUTCOME_RANK.get(a, 0) >= _OUTCOME_RANK.get(b, 0) else b


def h2_attempts_to_land(folded: list[FoldedRun]) -> dict[str, Any]:
    """For each merged PR, the number of builder runs before merge (distribution)."""
    builder_runs_by_pr: dict[int, int] = {}
    merged_prs = {
        pr for pr, o in h1_merge_rate(folded)["by_pr"].items() if o == "merged"
    }
    for r in folded:
        if r.pr_number in merged_prs and r.role == "builder":
            builder_runs_by_pr[r.pr_number] = builder_runs_by_pr.get(r.pr_number, 0) + 1

    attempts = sorted(builder_runs_by_pr.values())
    if not attempts:
        return {"merged_prs": 0, "first_attempt_pct": 0.0, "median_attempts": 0, "max_attempts": 0,
                "distribution": {}}
    first_try = sum(1 for a in attempts if a == 1)
    dist: dict[str, int] = {}
    for a in attempts:
        dist[str(a)] = dist.get(str(a), 0) + 1
    return {
        "merged_prs": len(attempts),
        "first_attempt_pct": _pct(first_try, len(attempts)),
        "median_attempts": int(statistics.median(attempts)),
        "max_attempts": max(attempts),
        "distribution": dist,
    }


def h3_clean_exit_rate(folded: list[FoldedRun]) -> dict[str, Any]:
    """Runs that exited normally ÷ all runs (execution / infra health)."""
    total = len(folded)
    clean = sum(1 for r in folded if r.clean_exit == "clean")
    return {
        "clean_exit_rate_pct": _pct(clean, total),
        "clean_runs": clean,
        "total_runs": total,
    }


def h4_process_friction(folded: list[FoldedRun]) -> dict[str, Any]:
    """% of runs with elevated friction + the per-run counts (leading indicator)."""
    total = len(folded)
    elevated = sum(1 for r in folded if r.elevated_friction)
    return {
        "elevated_friction_pct": _pct(elevated, total),
        "elevated_runs": elevated,
        "total_runs": total,
        "threshold": FRICTION_THRESHOLD,
    }


def cost_tokens_per_merged_pr(folded: list[FoldedRun]) -> dict[str, Any]:
    """Tokens per merged PR, per agent role (budget line; rework cost included)."""
    merged_prs = {pr for pr, o in h1_merge_rate(folded)["by_pr"].items() if o == "merged"}
    per_pr: dict[int, int] = {}
    per_role: dict[str, int] = {}
    for r in folded:
        if r.pr_number in merged_prs:
            per_pr[r.pr_number] = per_pr.get(r.pr_number, 0) + r.total_tokens
            per_role[r.role] = per_role.get(r.role, 0) + r.total_tokens
    total = sum(per_pr.values())
    return {
        "merged_prs": len(merged_prs),
        "total_tokens": total,
        "avg_tokens_per_merged_pr": int(total / len(merged_prs)) if merged_prs else 0,
        "tokens_per_role": per_role,
        "tokens_per_pr": per_pr,
    }


def triage_queue(folded: list[FoldedRun]) -> list[dict[str, Any]]:
    """Rank runs for inspection.

    Two interesting buckets: fragile successes (high friction + merged)
    and surprising failures (low friction + failed). Sorted by friction desc so
    the operator reads the noisiest transcripts first.
    """
    rows = []
    for r in sorted(folded, key=lambda x: x.friction_score, reverse=True):
        flag = ""
        landed = r.outcome == "merged"
        failed = r.outcome in ("closed_unmerged", "ci_failed", "reverted")
        if r.elevated_friction and landed:
            flag = "fragile-success"
        elif (not r.elevated_friction) and failed:
            flag = "surprising-failure"
        rows.append({
            "run_key": r.run_key,
            "role": r.role,
            "pr_number": r.pr_number,
            "friction_score": r.friction_score,
            "outcome": r.outcome,
            "clean_exit": r.clean_exit,
            "flag": flag,
        })
    return rows


def compute_summary(folded: list[FoldedRun]) -> dict[str, Any]:
    return {
        "H1_task_success": h1_merge_rate(folded),
        "H2_attempts_to_land": h2_attempts_to_land(folded),
        "H3_clean_exit": h3_clean_exit_rate(folded),
        "H4_process_friction": h4_process_friction(folded),
        "cost_tokens_per_merged_pr": cost_tokens_per_merged_pr(folded),
        "secondary": {
            "reviewer_changes_requested": sum(
                1 for r in folded if r.reviewer_verdict == "changes_requested"
            ),
            "reverts": sum(1 for r in folded if r.outcome == "reverted"),
        },
    }


def load_and_summarize(
    metrics_dir: Path | None = None, *, start: str | None = None, end: str | None = None
) -> dict[str, Any]:
    events = load_events(corpus_files(metrics_dir))
    folded = fold(events, start=start, end=end)
    return compute_summary(folded)


# ── Operator CLI / report rendering ────────────────────────────────────────────

def render_report(summary: dict[str, Any]) -> str:
    """Render the health quartet + cost line as a readable text table."""
    h1 = summary["H1_task_success"]
    h2 = summary["H2_attempts_to_land"]
    h3 = summary["H3_clean_exit"]
    h4 = summary["H4_process_friction"]
    cost = summary["cost_tokens_per_merged_pr"]
    sec = summary["secondary"]

    lines = [
        "Agent Observability — Health Summary",
        "=" * 40,
        "",
        f"H1  Task success (merge) rate : {h1['merge_rate_pct']:>5}%   "
        f"({h1['merged_prs']}/{h1['settled_prs']} settled; {h1['provisional_prs']} provisional)",
        f"H2  Attempts-to-land          : first-try {h2['first_attempt_pct']:>5}%   "
        f"median {h2['median_attempts']}  max {h2['max_attempts']}  "
        f"(over {h2['merged_prs']} merged PRs)",
        f"H3  Clean-exit rate (infra)   : {h3['clean_exit_rate_pct']:>5}%   "
        f"({h3['clean_runs']}/{h3['total_runs']} runs)",
        f"H4  Elevated-friction runs    : {h4['elevated_friction_pct']:>5}%   "
        f"({h4['elevated_runs']}/{h4['total_runs']} runs; threshold {h4['threshold']})",
        "",
        f"Cost  tokens / merged PR      : {cost['avg_tokens_per_merged_pr']:,} avg   "
        f"({cost['total_tokens']:,} total over {cost['merged_prs']} merged PRs)",
        f"      per role               : "
        + (", ".join(f"{r}={t:,}" for r, t in sorted(cost["tokens_per_role"].items())) or "—"),
        "",
        f"Secondary  reviewer changes-requested: {sec['reviewer_changes_requested']}   "
        f"reverts: {sec['reverts']}",
    ]
    # The divergence alarm: high infra health, low task success.
    if h3["clean_exit_rate_pct"] >= 80.0 and h1["settled_prs"] and h1["merge_rate_pct"] < 50.0:
        lines += ["", "⚠  DIVERGENCE: clean-exit high but merge rate low — "
                       "agents run fine but produce bad work. Inspect the triage queue."]
    return "\n".join(lines)


def render_triage(rows: list[dict[str, Any]], limit: int = 20) -> str:
    """Render the friction triage queue (highest-friction runs first)."""
    if not rows:
        return "Triage queue empty (no runs in window)."
    out = ["Triage queue — highest-friction runs first", "-" * 60,
           f"{'friction':>8}  {'role':<9} {'PR':>5}  {'outcome':<16} {'flag':<18} run_key"]
    for r in rows[:limit]:
        out.append(
            f"{r['friction_score']:>8}  {r['role']:<9} "
            f"{(r['pr_number'] or '-'):>5}  {r['outcome']:<16} {r['flag'] or '':<18} {r['run_key']}"
        )
    return "\n".join(out)


def main() -> int:
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Aggregate the ops-metrics corpus.")
    parser.add_argument("--start", default=None, help="ISO start of window (inclusive).")
    parser.add_argument("--end", default=None, help="ISO end of window (inclusive).")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON summary.")
    parser.add_argument("--triage", action="store_true", help="Show the friction triage queue.")
    parser.add_argument(
        "--metrics-dir",
        default=None,
        help="Path to the ops-metrics JSONL directory (overrides METRICS_DIR env).",
    )
    args = parser.parse_args()

    mdir = Path(args.metrics_dir) if args.metrics_dir else None
    events = load_events(corpus_files(mdir))
    folded = fold(events, start=args.start, end=args.end)

    if args.triage:
        print(render_triage(triage_queue(folded)))
        return 0

    summary = compute_summary(folded)
    if args.json:
        print(_json.dumps(summary, indent=2, default=str))
    else:
        print(render_report(summary))
    return 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())
