"""
bootstrap/state.py — Bootstrap progress state tracking.

Writes and reads .agentOS-state.json in the current working directory.
Tracks which steps have completed so a failed run can resume from where it stopped.

State file format:
{
  "spec_fingerprint": "sha256:...",
  "repo": "owner/repo",
  "created_at": "2026-06-26T10:00:00Z",
  "updated_at": "2026-06-26T10:02:00Z",
  "steps": {
    "labels":    {"status": "complete", "at": "..."},
    "board":     {"status": "failed",   "at": "...", "error": "..."},
    "workflows": {"status": "pending"},
    "scaffold":  {"status": "pending"},
    "apps":      {"status": "pending"}
  }
}

Status values: pending | complete | failed | skipped
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

STATE_FILE = ".agentOS-state.json"
STEP_NAMES = ("labels", "board", "workflows", "scaffold", "instrument", "apps")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BootstrapState:
    """Read/write wrapper around .agentOS-state.json."""

    def __init__(self, path: Path = Path(STATE_FILE)):
        self.path = path
        self._data: dict[str, Any] = {}

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                log.debug("Loaded state from %s", self.path)
            except Exception as exc:
                log.warning("Could not load state file (%s) — starting fresh", exc)
                self._data = {}
        else:
            self._data = {}

    def save(self) -> None:
        self._data["updated_at"] = _now()
        self.path.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")

    def init(self, repo: str, spec_fingerprint: str) -> None:
        """Initialise state for a fresh or reset run."""
        self._data = {
            "spec_fingerprint": spec_fingerprint,
            "repo": repo,
            "created_at": _now(),
            "updated_at": _now(),
            "steps": {name: {"status": "pending"} for name in STEP_NAMES},
        }
        self.save()

    def needs_reset(self, repo: str, spec_fingerprint: str) -> bool:
        """Return True if the state was for a different repo or spec version."""
        return (
            self._data.get("repo") != repo
            or self._data.get("spec_fingerprint") != spec_fingerprint
        )

    def step_status(self, step: str) -> str:
        return self._data.get("steps", {}).get(step, {}).get("status", "pending")

    def mark_complete(self, step: str) -> None:
        self._data.setdefault("steps", {})[step] = {"status": "complete", "at": _now()}
        self.save()
        log.info("Step '%s' complete", step)

    def mark_skipped(self, step: str, reason: str = "") -> None:
        self._data.setdefault("steps", {})[step] = {
            "status": "skipped", "at": _now(), "reason": reason
        }
        self.save()
        log.info("Step '%s' skipped: %s", step, reason)

    def mark_failed(self, step: str, error: str) -> None:
        self._data.setdefault("steps", {})[step] = {
            "status": "failed", "at": _now(), "error": error
        }
        self.save()
        log.error("Step '%s' failed: %s", step, error)

    def should_run(self, step: str) -> bool:
        """Return True if step needs to run (pending or previously failed)."""
        return self.step_status(step) in ("pending", "failed")

    def summary(self) -> str:
        steps = self._data.get("steps", {})
        parts = [f"{name}:{info.get('status', 'pending')}" for name, info in steps.items()]
        return "  ".join(parts)

    @property
    def repo(self) -> Optional[str]:
        return self._data.get("repo")

    @property
    def all_complete(self) -> bool:
        return all(
            self._data.get("steps", {}).get(s, {}).get("status") in ("complete", "skipped")
            for s in STEP_NAMES
        )
