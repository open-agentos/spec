"""
tests/test_release_consistency.py — Guards against the versioning drift that
motivated this file: bootstrap/agentOS.yaml and bootstrap/templates/ (the
copies actually shipped inside the PyPI package) silently falling out of sync
with the canonical root agentOS.yaml / templates/, and the package version,
spec version, and CHANGELOG disagreeing about what the "current" release is.

These are sync/consistency checks, not content checks — they don't assert
what specVersion *should* be, only that all the places it's recorded agree
with each other.
"""

from __future__ import annotations

import filecmp
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_bootstrap_agentos_yaml_matches_root():
    """bootstrap/agentOS.yaml is what `agentOS init` (no --from) and the
    PyPI package ship. It must be byte-identical to the canonical root
    agentOS.yaml, or users get a different spec depending on install path."""
    root = REPO_ROOT / "agentOS.yaml"
    bundled = REPO_ROOT / "bootstrap" / "agentOS.yaml"
    assert _read_text(root) == _read_text(bundled), (
        "bootstrap/agentOS.yaml has drifted from root agentOS.yaml. "
        "Run: cp agentOS.yaml bootstrap/agentOS.yaml"
    )


def test_bootstrap_templates_match_root():
    """bootstrap/templates/ must mirror root templates/ exactly — same files,
    same content — since it's the bundled copy used by the installed CLI."""
    root_dir = REPO_ROOT / "templates"
    bundled_dir = REPO_ROOT / "bootstrap" / "templates"

    root_files = {p.relative_to(root_dir) for p in root_dir.rglob("*") if p.is_file()}
    bundled_files = {p.relative_to(bundled_dir) for p in bundled_dir.rglob("*") if p.is_file()}

    assert root_files == bundled_files, (
        f"File sets differ.\nOnly in root: {root_files - bundled_files}\n"
        f"Only in bootstrap: {bundled_files - root_files}"
    )

    mismatched = [
        str(rel) for rel in sorted(root_files)
        if not filecmp.cmp(root_dir / rel, bundled_dir / rel, shallow=False)
    ]
    assert not mismatched, (
        f"bootstrap/templates/ differs from templates/ for: {mismatched}. "
        "Run: rm -rf bootstrap/templates && cp -r templates bootstrap/templates"
    )


def test_bootstrap_schema_matches_root():
    root_dir = REPO_ROOT / "schema"
    bundled_dir = REPO_ROOT / "bootstrap" / "schema"
    root_files = {p.relative_to(root_dir) for p in root_dir.rglob("*") if p.is_file()}
    bundled_files = {p.relative_to(bundled_dir) for p in bundled_dir.rglob("*") if p.is_file()}
    assert root_files == bundled_files
    mismatched = [
        str(rel) for rel in sorted(root_files)
        if not filecmp.cmp(root_dir / rel, bundled_dir / rel, shallow=False)
    ]
    assert not mismatched, f"bootstrap/schema/ differs from schema/ for: {mismatched}"


def test_pyproject_version_matches_bootstrap_version():
    pyproject = tomllib.loads(_read_text(REPO_ROOT / "pyproject.toml"))
    pkg_version = pyproject["project"]["version"]

    init_text = _read_text(REPO_ROOT / "bootstrap" / "__init__.py")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert match, "bootstrap/__init__.py has no __version__ assignment"

    assert pkg_version == match.group(1), (
        f"pyproject.toml version ({pkg_version}) != "
        f"bootstrap.__version__ ({match.group(1)})"
    )


def test_changelog_latest_entry_matches_package_version():
    pyproject = tomllib.loads(_read_text(REPO_ROOT / "pyproject.toml"))
    pkg_version = pyproject["project"]["version"]

    changelog = _read_text(REPO_ROOT / "CHANGELOG.md")
    # First "## [x.y.z]" heading is the latest entry by convention.
    match = re.search(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
    assert match, "CHANGELOG.md has no version heading"
    latest = match.group(1)

    assert latest == pkg_version, (
        f"CHANGELOG.md latest entry is [{latest}] but pyproject.toml version "
        f"is {pkg_version}. Either cut a CHANGELOG entry or bump the version."
    )


def test_root_and_bootstrap_specversion_agree():
    root_spec = yaml.safe_load(_read_text(REPO_ROOT / "agentOS.yaml"))
    bundled_spec = yaml.safe_load(_read_text(REPO_ROOT / "bootstrap" / "agentOS.yaml"))
    assert root_spec["specVersion"] == bundled_spec["specVersion"]


def test_no_stale_v1_0_install_refs():
    """Every `@v1.0` (bare, no patch) reference to this repo as an install
    source is a tag that doesn't exist (only v1.0.0-alpha ever did) and a
    likely copy-paste of an early draft. New refs should pin a real tag."""
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        if path.suffix not in {".md", ".yaml", ".yml"}:
            continue
        text = _read_text(path)
        if re.search(r"open-agentos/spec[^\n]*@v1\.0(?!\.\d)", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"Stale @v1.0 spec install refs found in: {offenders}"


def test_install_instructions_match_pyproject_package_name():
    """`pip install <name>` / `uv tool install <name>` in docs must match the
    actual registered PyPI project name in pyproject.toml. This caught a real
    bug: docs said `agentOS-cli`, pyproject.toml said `open-agentos`, and
    neither matched the name PyPI actually accepted (`open-agentos-cli`)."""
    pyproject = tomllib.loads(_read_text(REPO_ROOT / "pyproject.toml"))
    pkg_name = pyproject["project"]["name"]

    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix != ".md":
            continue
        text = _read_text(path)
        for match in re.finditer(
            r"(?:pip|uv tool) install ([A-Za-z0-9][A-Za-z0-9._-]*)", text
        ):
            name = match.group(1)
            # Ignore obvious non-package args / flags people pip install in examples.
            if name in {"-e", "build", "twine", "."}:
                continue
            if name != pkg_name:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: installs '{name}'")

    assert not offenders, (
        f"Install instructions don't match pyproject.toml name ({pkg_name!r}): "
        + "; ".join(offenders)
    )
