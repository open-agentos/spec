"""
bootstrap/upgrade.py — agentOS apply --upgrade logic.

Upgrades files in a provisioned repository from one spec version to another by
applying only the changes inside managed blocks, leaving user content untouched.

## Managed-block protocol

Files under upgrade management are fenced with HTML comment markers:

    <!-- agentOS:managed:begin role=<id> hash=<sha256_hex_8> -->
    ... managed content ...
    <!-- agentOS:managed:end -->

The ``hash`` attribute is the first 8 hex digits of SHA-256(managed content).
It is the "last-known-clean" hash: the hash of the content as it was when the
spec last wrote it.

### Upgrade decision per block

For each managed block in a file:

1. Render the new template block (from the target spec version).
2. Compute ``new_hash = sha256(new_content)[:8]``.
3. Compute ``current_hash = sha256(current_content)[:8]``.
4. Read ``stored_hash`` from the begin marker.

Decision table:
  current_hash == stored_hash AND current_hash == new_hash  →  no-op (already current)
  current_hash == stored_hash AND current_hash != new_hash  →  apply update
  current_hash != stored_hash                               →  CONFLICT (user edited)
      regardless of new_hash — write to upgrade-conflicts.yaml, leave block unchanged

### Files in scope (default)

  agentOS.yaml                               managed per top-level section
  .github/workflows/agent-orchestrator.yml   managed per job block
  agents/*/AGENT.md                          full file, single block
  field-bindings.json                        fully regenerated (no markers)

Public API
----------
  run_upgrade(opts: UpgradeOptions) -> UpgradeResult
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from bootstrap.workflows import _build_vars, _expand

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANAGED_BEGIN_RE = re.compile(
    r"<!--\s*agentOS:managed:begin\s+([^>]*?)\s*-->",
    re.IGNORECASE,
)
MANAGED_END = "<!-- agentOS:managed:end -->"

_GITHUB_API = "https://api.github.com"
_SPEC_REPO = "open-agentos/spec"

# Files the upgrade engine manages.
# Each entry: (path_glob_relative_to_target, template_path_relative_to_spec_templates)
# None template_path means "fully regenerated without markers" (field-bindings.json).
_MANAGED_FILES: list[tuple[str, Optional[str]]] = [
    ("agentOS.yaml", "agentOS.yaml"),
    (".github/workflows/agent-orchestrator.yml", "workflows/agent-orchestrator.yml"),
    ("agents/builder/AGENT.md", "agents/builder/AGENT.md.template"),
    ("agents/reviewer/AGENT.md", "agents/reviewer/AGENT.md.template"),
    ("agents/watcher/AGENT.md", "agents/watcher/AGENT.md.template"),
    ("agents/planner/AGENT.md", "agents/planner/AGENT.md.template"),
    ("agents/docs/AGENT.md", "agents/docs/AGENT.md.template"),
    ("field-bindings.json", None),  # fully regenerated
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class UpgradeOptions:
    """Options for the upgrade command."""
    target_dir: Path                          # root of the provisioned target repo
    templates_dir: Optional[Path] = None      # local spec templates/ dir (or fetched)
    from_version: Optional[str] = None        # current spec version (read from agentOS.yaml)
    to_version: Optional[str] = None          # target version (default: latest tag)
    to_version_explicit: bool = False         # True when --to was passed explicitly
    dry_run: bool = False
    repo: Optional[str] = None               # "owner/repo" for --repo mode (GitHub API)
    token: Optional[str] = None              # GitHub token for --repo mode
    spec: Optional[dict[str, Any]] = None    # parsed agentOS.yaml (already loaded)


@dataclass
class BlockConflict:
    """A managed block that has been user-edited and cannot be auto-upgraded."""
    file: str
    block_id: str
    stored_hash: str
    current_hash: str
    reason: str = "user-edited content detected"


@dataclass
class FileChange:
    """A file that was (or would be) modified."""
    path: str
    blocks_updated: int = 0
    blocks_skipped: int = 0  # already current
    unified_diff: str = ""


@dataclass
class UpgradeResult:
    """Result of a complete upgrade run."""
    from_version: str = ""
    to_version: str = ""
    files_changed: list[FileChange] = field(default_factory=list)
    files_unchanged: list[str] = field(default_factory=list)
    blocks_skipped: int = 0   # already-current blocks (no-op)
    conflicts: list[BlockConflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def print_summary(self) -> None:
        label = "[dry-run] " if self.dry_run else ""
        print(f"\n{label}Upgrade summary: {self.from_version} → {self.to_version}")
        if self.files_changed:
            print(f"  Files changed ({len(self.files_changed)}):")
            for fc in self.files_changed:
                print(f"    • {fc.path}  "
                      f"blocks_updated={fc.blocks_updated} "
                      f"blocks_skipped={fc.blocks_skipped}")
        else:
            print("  No files changed (already up to date).")
        if self.conflicts:
            print(f"  Conflicts ({len(self.conflicts)}) — written to "
                  f".agentOS/upgrade-conflicts.yaml:")
            for c in self.conflicts:
                print(f"    ⚠  {c.file}  block={c.block_id}  ({c.reason})")
        if self.files_unchanged:
            print(f"  Unchanged: {', '.join(self.files_unchanged)}")
        if self.errors:
            print("  Errors:")
            for e in self.errors:
                print(f"    ✗ {e}")
        if self.dry_run and self.files_changed:
            print("\nRun without --dry-run to apply.")


# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------

def _sha256_short(text: str) -> str:
    """Return the first 8 hex digits of SHA-256(text encoded as UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _sha256_full(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Managed-block parsing and building
# ---------------------------------------------------------------------------

def parse_attributes(attr_string: str) -> dict[str, str]:
    """Parse ``key=value`` pairs from a managed-begin marker attribute string.

    Handles:
        role=planner hash=abc12345
        role=orchestrator-job name=plan-orchestrator hash=deadbeef
    """
    attrs: dict[str, str] = {}
    for m in re.finditer(r"(\w+)=([^\s]+)", attr_string):
        attrs[m.group(1)] = m.group(2)
    return attrs


def build_begin_marker(attrs: dict[str, str]) -> str:
    """Build a ``<!-- agentOS:managed:begin ... -->`` marker string."""
    attr_str = " ".join(f"{k}={v}" for k, v in attrs.items())
    return f"<!-- agentOS:managed:begin {attr_str} -->"


def split_managed_blocks(content: str) -> list[tuple[str, Optional[dict[str, str]]]]:
    """Split ``content`` into alternating non-managed and managed segments.

    Returns a list of ``(text, attrs_or_None)`` pairs where ``attrs`` is None for
    non-managed segments and a dict of the begin-marker attributes for managed ones.

    The managed content includes everything between (exclusive) the begin and end
    markers. The markers themselves are NOT included in the segment text.

    Example output for a file with one managed block::

        [
            ("preamble\\n", None),
            ("managed content\\n", {"role": "planner", "hash": "abc12345"}),
            ("\\npostamble", None),
        ]
    """
    segments: list[tuple[str, Optional[dict[str, str]]]] = []
    pos = 0

    for begin_match in MANAGED_BEGIN_RE.finditer(content):
        # Non-managed text before this block.
        if begin_match.start() > pos:
            segments.append((content[pos:begin_match.start()], None))

        attrs = parse_attributes(begin_match.group(1))

        end_idx = content.find(MANAGED_END, begin_match.end())
        if end_idx == -1:
            # Malformed: no closing marker — treat rest of file as non-managed.
            log.warning("Managed block opened at offset %d has no closing marker",
                        begin_match.start())
            segments.append((content[begin_match.start():], None))
            pos = len(content)
            break

        # Managed content between begin and end markers (strip leading newline after begin).
        managed_raw = content[begin_match.end():end_idx]
        segments.append((managed_raw, attrs))

        pos = end_idx + len(MANAGED_END)

    # Remaining non-managed tail.
    if pos < len(content):
        segments.append((content[pos:], None))

    return segments


def reassemble(segments: list[tuple[str, Optional[dict[str, str]]]]) -> str:
    """Reconstruct file content from segments produced by ``split_managed_blocks``.

    Managed segments get begin/end markers re-injected around them.
    """
    parts: list[str] = []
    for text, attrs in segments:
        if attrs is None:
            parts.append(text)
        else:
            parts.append(build_begin_marker(attrs) + text + MANAGED_END)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Block-level upgrade logic
# ---------------------------------------------------------------------------

@dataclass
class BlockUpgradeDecision:
    action: str          # "update" | "skip" | "conflict" | "error"
    new_content: str = ""
    new_attrs: dict[str, str] = field(default_factory=dict)
    conflict: Optional[BlockConflict] = None
    reason: str = ""


def decide_block_upgrade(
    file_path: str,
    current_content: str,
    attrs: dict[str, str],
    new_template_content: Optional[str],
) -> BlockUpgradeDecision:
    """Decide what to do with one managed block.

    Args:
        file_path:            Display path (for conflict messages).
        current_content:      The text currently between the begin/end markers.
        attrs:                Parsed attributes from the begin marker.
        new_template_content: The rendered new content for this block from the
                              upgraded spec, or None if the block is being removed.

    Returns:
        BlockUpgradeDecision with action and payload.
    """
    block_id = attrs.get("role") or attrs.get("name") or "unknown"
    stored_hash = attrs.get("hash", "")
    current_hash = _sha256_short(current_content)

    if stored_hash and current_hash != stored_hash:
        # User has edited this block — never overwrite silently.
        conflict = BlockConflict(
            file=file_path,
            block_id=block_id,
            stored_hash=stored_hash,
            current_hash=current_hash,
        )
        return BlockUpgradeDecision(
            action="conflict",
            conflict=conflict,
            reason=f"hash mismatch: stored={stored_hash} current={current_hash}",
        )

    if new_template_content is None:
        # Block is being removed in the new version.
        return BlockUpgradeDecision(action="remove", reason="block removed in new spec")

    new_hash = _sha256_short(new_template_content)
    if new_hash == current_hash:
        # Already current — no-op.
        return BlockUpgradeDecision(
            action="skip",
            new_content=current_content,
            new_attrs=attrs,
            reason="already current",
        )

    # Apply the update.
    new_attrs = dict(attrs)
    new_attrs["hash"] = new_hash
    return BlockUpgradeDecision(
        action="update",
        new_content=new_template_content,
        new_attrs=new_attrs,
        reason=f"hash changed: {current_hash} → {new_hash}",
    )


# ---------------------------------------------------------------------------
# File-level upgrade logic
# ---------------------------------------------------------------------------

def upgrade_file(
    file_path: str,
    current_content: str,
    new_template_content: str,
    dry_run: bool = False,
) -> tuple[str, FileChange, list[BlockConflict]]:
    """Apply managed-block upgrades to one file.

    Merges ``new_template_content`` into ``current_content`` by:
    - Replacing managed blocks that are clean (hash matches stored).
    - Skipping managed blocks that are already current.
    - Flagging managed blocks with user edits as conflicts.
    - Leaving all non-managed content untouched.

    Returns:
        (updated_content, FileChange, conflicts)
        ``updated_content`` is byte-identical to ``current_content`` when there
        are no changes or only conflicts.
    """
    changes = FileChange(path=file_path)
    conflicts: list[BlockConflict] = []

    # Parse new template into its managed blocks keyed by block_id.
    new_segments = split_managed_blocks(new_template_content)
    new_blocks: dict[str, str] = {}
    for text, attrs in new_segments:
        if attrs is not None:
            block_id = attrs.get("role") or attrs.get("name") or ""
            if block_id:
                new_blocks[block_id] = text

    # Parse current file.
    current_segments = split_managed_blocks(current_content)

    updated_segments: list[tuple[str, Optional[dict[str, str]]]] = []
    content_changed = False

    for text, attrs in current_segments:
        if attrs is None:
            # Non-managed — preserve verbatim.
            updated_segments.append((text, None))
            continue

        block_id = attrs.get("role") or attrs.get("name") or ""
        new_block_text = new_blocks.get(block_id)

        decision = decide_block_upgrade(
            file_path=file_path,
            current_content=text,
            attrs=attrs,
            new_template_content=new_block_text,
        )

        if decision.action == "update":
            updated_segments.append((decision.new_content, decision.new_attrs))
            changes.blocks_updated += 1
            content_changed = True
            log.info("upgrade %s block=%s  (%s)", file_path, block_id, decision.reason)
        elif decision.action == "skip":
            updated_segments.append((text, attrs))
            changes.blocks_skipped += 1
            log.debug("skip %s block=%s  (already current)", file_path, block_id)
        elif decision.action == "conflict":
            # Leave existing content unchanged.
            updated_segments.append((text, attrs))
            if decision.conflict is not None:
                conflicts.append(decision.conflict)
            log.warning("conflict %s block=%s  (%s)", file_path, block_id, decision.reason)
        elif decision.action == "remove":
            # Drop the block (and its markers) from the output.
            content_changed = True
            changes.blocks_updated += 1
            log.info("remove %s block=%s", file_path, block_id)
            # Don't append — the block disappears.

    updated_content = reassemble(updated_segments)

    if content_changed:
        changes.unified_diff = "".join(difflib.unified_diff(
            current_content.splitlines(keepends=True),
            updated_content.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        ))

    return updated_content, changes, conflicts


# ---------------------------------------------------------------------------
# Template fetching (from GitHub or local)
# ---------------------------------------------------------------------------

def _fetch_latest_tag(spec_repo: str = _SPEC_REPO) -> str:
    """Fetch the latest release tag from GitHub API."""
    url = f"{_GITHUB_API}/repos/{spec_repo}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name", "")
        log.info("Latest tag: %s", tag)
        return tag
    except Exception as exc:
        log.warning("Could not fetch latest tag: %s", exc)
        return ""


def _fetch_file_from_github(
    spec_repo: str,
    git_ref: str,
    file_path: str,
) -> Optional[str]:
    """Fetch a file from GitHub raw content."""
    url = (f"https://raw.githubusercontent.com/{spec_repo}/{git_ref}/{file_path}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return resp.read().decode("utf-8")
    except Exception as exc:
        log.warning("Could not fetch %s@%s: %s", file_path, git_ref, exc)
        return None


def _resolve_templates_dir(opts: UpgradeOptions) -> Optional[Path]:
    """Return a local templates/ directory, downloading from GitHub if needed.

    Resolution order:
    1. Explicit --templates-dir from opts (must exist).
    2. importlib.resources — works with pip/pipx installs (including zip-based).
    3. Path(__file__).parent / "templates" — editable / source installs.
    4. Repo root / "templates" — running directly from the cloned repo.
    """
    if opts.templates_dir and opts.templates_dir.exists():
        return opts.templates_dir

    # --- 2. importlib.resources (pip/pipx compatible) ---
    # importlib.resources.files() works with both directory-based and zip-based
    # package installations. It returns a traversable that can be converted to a
    # real path via as_file() context manager, but for our purposes we just need
    # to check existence and return the path.
    try:
        from importlib.resources import files as _res_files
        pkg_templates = _res_files("bootstrap") / "templates"
        try:
            # For directory-based installs, str() gives us the real path.
            tpl_path = Path(str(pkg_templates))
            if tpl_path.exists() and tpl_path.is_dir():
                log.debug("Templates found via importlib.resources: %s", tpl_path)
                return tpl_path
        except (TypeError, AttributeError):
            pass
    except (ImportError, ModuleNotFoundError):
        pass

    # --- 3. __file__-relative (editable installs) ---
    bundled = Path(__file__).resolve().parent / "templates"
    if bundled.exists():
        return bundled

    # --- 4. Repo root (running from source clone) ---
    repo_root = Path(__file__).resolve().parent.parent
    root_templates = repo_root / "templates"
    if root_templates.exists():
        return root_templates

    return None


# ---------------------------------------------------------------------------
# Receipt and conflict output
# ---------------------------------------------------------------------------

def _write_upgrade_receipt(
    result: UpgradeResult,
    target_dir: Path,
) -> None:
    """Write .agentOS/upgrade-receipt.yaml."""
    receipt_path = target_dir / ".agentOS" / "upgrade-receipt.yaml"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "from_version": result.from_version,
        "to_version": result.to_version,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": result.dry_run,
        "files_changed": [
            {
                "path": fc.path,
                "blocks_updated": fc.blocks_updated,
                "blocks_skipped": fc.blocks_skipped,
            }
            for fc in result.files_changed
        ],
        "blocks_skipped_total": result.blocks_skipped,
        "conflicts": len(result.conflicts),
        "errors": result.errors,
    }
    receipt_path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    log.info("Wrote upgrade receipt: %s", receipt_path)


def _write_upgrade_conflicts(
    conflicts: list[BlockConflict],
    target_dir: Path,
) -> None:
    """Write .agentOS/upgrade-conflicts.yaml."""
    if not conflicts:
        return
    conflicts_path = target_dir / ".agentOS" / "upgrade-conflicts.yaml"
    conflicts_path.parent.mkdir(parents=True, exist_ok=True)

    data = [
        {
            "file": c.file,
            "block_id": c.block_id,
            "stored_hash": c.stored_hash,
            "current_hash": c.current_hash,
            "reason": c.reason,
        }
        for c in conflicts
    ]
    conflicts_path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    log.info("Wrote %d conflict(s) to %s", len(conflicts), conflicts_path)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def run_upgrade(opts: UpgradeOptions) -> UpgradeResult:
    """Run the upgrade sequence.

    Steps:
    1. Resolve from_version (from local agentOS.yaml specVersion).
    2. Resolve to_version (from --to flag or latest GitHub tag).
    3. If already current: write receipt and return.
    4. For each managed file: fetch new template, diff managed blocks,
       apply clean changes, collect conflicts.
    5. Write .agentOS/upgrade-receipt.yaml (always, even on dry-run).
    6. Write .agentOS/upgrade-conflicts.yaml (if any).

    Args:
        opts:  UpgradeOptions controlling paths, versions, and dry-run mode.

    Returns:
        UpgradeResult with per-file changes, conflicts, and metadata.
    """
    result = UpgradeResult(dry_run=opts.dry_run)
    target_dir = opts.target_dir

    # ---- 1. Resolve from_version ----
    spec = opts.spec
    if spec is None:
        spec_file = target_dir / "agentOS.yaml"
        if spec_file.exists():
            with open(spec_file, encoding="utf-8") as f:
                spec = yaml.safe_load(f)
        else:
            result.errors.append("agentOS.yaml not found in target directory")
            return result

    result.from_version = spec.get("specVersion", "unknown")

    # ---- 2. Resolve to_version ----
    to_version = opts.to_version
    if not to_version:
        to_version = _fetch_latest_tag()
    if not to_version:
        # --to was not provided AND the tag fetch failed.
        # Never silently no-op — the operator must know something is wrong.
        if opts.to_version_explicit:
            # Should not reach here (to_version_explicit means to_version was set),
            # but guard anyway.
            result.errors.append("--to version was passed but resolved to empty string.")
        else:
            result.errors.append(
                "Could not determine target version. "
                "Pass --to VERSION explicitly, or ensure open-agentos/spec has a "
                "published release tag."
            )
        if not opts.dry_run:
            _write_upgrade_receipt(result, target_dir)
        return result
    result.to_version = to_version

    # ---- 3. Already current? ----
    if result.from_version == result.to_version:
        log.info("Already at version %s — nothing to do.", to_version)
        result.blocks_skipped = 0
        if not opts.dry_run:
            _write_upgrade_receipt(result, target_dir)
        return result

    # ---- 4. Resolve templates directory ----
    templates_dir = _resolve_templates_dir(opts)
    if templates_dir is None:
        result.errors.append(
            "Could not locate templates directory. "
            "Pass --templates-dir or install from the spec repo."
        )
        if not opts.dry_run:
            _write_upgrade_receipt(result, target_dir)
        return result

    log.info("Using templates from: %s", templates_dir)

    # Build template variable substitution map.
    template_vars = _build_vars(spec)

    # ---- 5. Process each managed file ----
    all_conflicts: list[BlockConflict] = []

    for target_rel, template_rel in _MANAGED_FILES:
        target_file = target_dir / target_rel

        if not target_file.exists():
            log.debug("Skipping %s (not present in target)", target_rel)
            continue

        # field-bindings.json: fully regenerated, no block diffing.
        if template_rel is None:
            _handle_fully_regenerated(
                target_file=target_file,
                target_rel=target_rel,
                spec=spec,
                result=result,
                dry_run=opts.dry_run,
            )
            continue

        # Fetch new template content.
        template_file = templates_dir / template_rel
        if not template_file.exists():
            # Try fetching from GitHub at the target version.
            new_template_raw = _fetch_file_from_github(
                _SPEC_REPO, to_version, f"templates/{template_rel}"
            )
            if new_template_raw is None:
                log.warning("Template not found for %s — skipping", target_rel)
                result.files_unchanged.append(target_rel)
                continue
        else:
            new_template_raw = template_file.read_text(encoding="utf-8")

        # Expand template variables.
        new_template = _expand(new_template_raw, template_vars)

        # Read current file.
        current_content = target_file.read_text(encoding="utf-8")

        # Check if the file has any managed blocks at all.
        has_managed = bool(MANAGED_BEGIN_RE.search(current_content))
        if not has_managed:
            # File predates managed-block instrumentation — skip with a note.
            log.info(
                "Skipping %s: no managed blocks found. "
                "Run `agentOS apply` first to instrument the file.",
                target_rel,
            )
            result.files_unchanged.append(target_rel)
            continue

        # Perform the managed-block upgrade.
        updated_content, file_change, file_conflicts = upgrade_file(
            file_path=target_rel,
            current_content=current_content,
            new_template_content=new_template,
            dry_run=opts.dry_run,
        )

        all_conflicts.extend(file_conflicts)
        result.blocks_skipped += file_change.blocks_skipped

        if file_change.blocks_updated > 0:
            result.files_changed.append(file_change)
            if opts.dry_run:
                if file_change.unified_diff:
                    print(file_change.unified_diff)
            else:
                target_file.write_text(updated_content, encoding="utf-8")
                log.info("Updated %s (%d block(s))", target_rel, file_change.blocks_updated)
        else:
            result.files_unchanged.append(target_rel)

    result.conflicts = all_conflicts

    # ---- 6. Write receipt and conflicts ----
    if not opts.dry_run:
        _write_upgrade_receipt(result, target_dir)
        _write_upgrade_conflicts(all_conflicts, target_dir)

    return result


# ---------------------------------------------------------------------------
# Field-bindings regeneration (no managed blocks)
# ---------------------------------------------------------------------------

def _handle_fully_regenerated(
    target_file: Path,
    target_rel: str,
    spec: dict[str, Any],
    result: UpgradeResult,
    dry_run: bool,
) -> None:
    """field-bindings.json is fully regenerated from the new spec fingerprint.

    We only touch it if the spec has changed in a way that affects board fields.
    The existing board provisioner handles the actual re-sync; here we just
    flag it as "changed" so the operator knows to run `agentOS apply --only board`.
    """
    # For field-bindings.json, we check the stored fingerprint against the
    # current spec's board field fingerprint.
    try:
        existing = json.loads(target_file.read_text(encoding="utf-8"))
        stored_fp = existing.get("schema_fingerprint", "")
    except Exception:
        stored_fp = ""

    from bootstrap.board import compute_fingerprint
    current_fp = "sha256:" + compute_fingerprint(spec)

    if stored_fp == current_fp:
        result.files_unchanged.append(target_rel)
        log.debug("field-bindings.json fingerprint matches — no board re-sync needed")
    else:
        fc = FileChange(
            path=target_rel,
            blocks_updated=1,
            unified_diff=(
                f"--- a/{target_rel}\n+++ b/{target_rel}\n"
                f"@@ fingerprint changed @@\n"
                f"-schema_fingerprint: {stored_fp}\n"
                f"+schema_fingerprint: {current_fp}\n"
                f"# Run: agentOS apply --only board  to re-sync board fields\n"
            ),
        )
        result.files_changed.append(fc)
        log.info(
            "field-bindings.json fingerprint changed (%s → %s). "
            "Run `agentOS apply --only board` to re-sync board fields.",
            stored_fp, current_fp,
        )


# ---------------------------------------------------------------------------
# Utilities for injecting managed-block markers into templates
# (used by `agentOS apply` to instrument files on first write)
# ---------------------------------------------------------------------------

def wrap_in_managed_block(
    content: str,
    role: str,
    extra_attrs: Optional[dict[str, str]] = None,
) -> str:
    """Wrap ``content`` in a managed block with a computed hash.

    Use this when writing a file for the first time so future upgrades
    can detect user edits via the stored hash.

    The hash is computed over the exact bytes that will appear between the
    begin and end markers (including the surrounding newlines added by this
    function), so that ``split_managed_blocks`` → ``decide_block_upgrade``
    sees a matching hash on the first upgrade check.

    Args:
        content:     The managed content (without markers or surrounding newlines).
        role:        The ``role=`` attribute value (e.g. "planner", "orchestrator").
        extra_attrs: Additional ``key=value`` pairs for the begin marker.

    Returns:
        The content with begin/end markers injected.
    """
    # The text that will sit between the markers (as split_managed_blocks sees it).
    inner = "\n" + content + "\n"
    attrs: dict[str, str] = {"role": role}
    if extra_attrs:
        attrs.update(extra_attrs)
    attrs["hash"] = _sha256_short(inner)
    return build_begin_marker(attrs) + inner + MANAGED_END


# ---------------------------------------------------------------------------
# Instrumentation — add managed-block markers to already-provisioned files
# ---------------------------------------------------------------------------

@dataclass
class InstrumentResult:
    """Result of an instrument run."""
    files_instrumented: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)  # already have markers
    files_missing: list[str] = field(default_factory=list)  # not present in target
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def print_summary(self) -> None:
        label = "[dry-run] " if self.dry_run else ""
        print(f"\n{label}Instrument summary:")
        if self.files_instrumented:
            print(f"  Instrumented ({len(self.files_instrumented)}):")
            for f in self.files_instrumented:
                print(f"    + {f}")
        if self.files_skipped:
            print(f"  Already instrumented ({len(self.files_skipped)}):")
            for f in self.files_skipped:
                print(f"    ~ {f}")
        if self.files_missing:
            print(f"  Not present (skipped): {', '.join(self.files_missing)}")
        if not self.files_instrumented and not self.files_skipped:
            print("  No managed files found in target directory.")
        if self.errors:
            print("  Errors:")
            for e in self.errors:
                print(f"    ✗ {e}")


def instrument_file(
    file_path: str,
    current_content: str,
    role: str,
) -> tuple[str, bool]:
    """Add managed-block markers to a file that has none.

    If the file already contains managed markers, returns (current_content, False)
    — it is never re-instrumented (idempotent).

    The entire file content becomes the managed block. Content is preserved
    byte-for-byte inside the markers; nothing is reformatted.

    Args:
        file_path:        Display path for logging.
        current_content:  The full current file text.
        role:             The ``role=`` value for the managed-block begin marker.

    Returns:
        (instrumented_content, was_changed)
        ``was_changed`` is False when the file already had markers.
    """
    if MANAGED_BEGIN_RE.search(current_content):
        log.debug("instrument_file: %s already has managed markers — skipping", file_path)
        return current_content, False

    # Wrap the entire file content. strip trailing newline before wrapping so
    # wrap_in_managed_block's padding produces clean \n boundaries.
    instrumented = wrap_in_managed_block(current_content.rstrip("\n"), role=role) + "\n"
    log.info("instrument_file: added managed markers to %s (role=%s)", file_path, role)
    return instrumented, True


def instrument_files(
    target_dir: Path,
    dry_run: bool = False,
    managed_files: Optional[list[tuple[str, Optional[str]]]] = None,
) -> InstrumentResult:
    """Add managed-block markers to all managed files in target_dir.

    Each file in ``managed_files`` (defaults to ``_MANAGED_FILES``) that:
    - exists in target_dir, and
    - does not already have managed-block markers

    is instrumented in place: its entire content becomes the managed block.
    Files that already have markers are left untouched (idempotent).

    Args:
        target_dir:    Root of the provisioned target repo.
        dry_run:       If True, log changes but do not write files.
        managed_files: Override the list of (target_rel, template_rel) pairs.
                       Defaults to the module-level ``_MANAGED_FILES``.

    Returns:
        InstrumentResult with per-file outcomes.
    """
    result = InstrumentResult(dry_run=dry_run)
    scope = managed_files if managed_files is not None else _MANAGED_FILES

    for target_rel, _template_rel in scope:
        # field-bindings.json is fully regenerated — no marker instrumentation.
        if _template_rel is None:
            continue

        target_file = target_dir / target_rel

        if not target_file.exists():
            log.debug("instrument_files: %s not present — skipping", target_rel)
            result.files_missing.append(target_rel)
            continue

        try:
            current_content = target_file.read_text(encoding="utf-8")
        except Exception as exc:
            result.errors.append(f"Could not read {target_rel}: {exc}")
            continue

        # Infer role from the first path segment of the target (e.g. "builder"
        # from "agents/builder/AGENT.md"). Fall back to the file stem.
        parts = Path(target_rel).parts
        role = parts[1] if len(parts) > 1 else Path(target_rel).stem

        try:
            instrumented, changed = instrument_file(
                file_path=target_rel,
                current_content=current_content,
                role=role,
            )
        except Exception as exc:
            result.errors.append(f"Could not instrument {target_rel}: {exc}")
            continue

        if not changed:
            result.files_skipped.append(target_rel)
        else:
            result.files_instrumented.append(target_rel)
            if not dry_run:
                try:
                    target_file.write_text(instrumented, encoding="utf-8")
                except Exception as exc:
                    result.errors.append(f"Could not write {target_rel}: {exc}")

    return result
