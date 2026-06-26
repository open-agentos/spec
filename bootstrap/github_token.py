"""
bootstrap/github_token.py — GitHub App installation token minter.

Generates short-lived GitHub App installation tokens for any role defined
in agentOS.yaml. Runtime-agnostic: no 3Qs-specific logic.

Usage (standalone):
    python3 -m bootstrap.github_token builder
    python3 -m bootstrap.github_token reviewer

Usage (library):
    from bootstrap.github_token import get_token, load_env
    token = get_token("builder", env_file=Path(".env"))

Credential resolution (per role; ROLE = BUILDER | REVIEWER | etc.):

  App ID:
    GITHUB_APP_ID_{ROLE}

  Private key (first match wins):
    GITHUB_APP_PRIVATE_KEY_CONTENT_{ROLE}  — raw PEM inline (GHA secret friendly;
                                              literal \\n sequences are honoured)
    GITHUB_APP_PRIVATE_KEY_{ROLE}          — path to a .pem file on disk

  Installation ID (optional):
    GITHUB_APP_INSTALLATION_ID_{ROLE}      — if unset, discovered dynamically via
                                              GET /repos/{owner}/{repo}/installation
                                              using TARGET_REPO env var.

Environment variables loaded from .env in the specified directory (or cwd).
Process environment always takes precedence over .env values.

Requires: PyJWT, cryptography, requests
    uv run --with PyJWT --with cryptography --with requests python3 -m bootstrap.github_token builder
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import jwt
import requests
from cryptography.hazmat.primitives.serialization import load_pem_private_key

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def load_env(env_path: Path) -> None:
    """Load a .env file into os.environ. Process env wins (setdefault)."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_private_key(role: str):
    """Return a loaded RSA private key for ROLE.

    Prefers inline PEM content (safe for GHA secrets, never touches disk)
    over an on-disk .pem file path.
    """
    role_u = role.upper()
    content = os.environ.get(f"GITHUB_APP_PRIVATE_KEY_CONTENT_{role_u}")
    if content:
        # GHA secrets may flatten newlines to literal \\n — restore them.
        if "\\n" in content and "\n" not in content.strip("\n"):
            content = content.replace("\\n", "\n")
        return load_pem_private_key(content.encode("utf-8"), password=None)

    key_path = os.environ.get(f"GITHUB_APP_PRIVATE_KEY_{role_u}")
    if not key_path:
        raise KeyError(
            f"Missing credential: set GITHUB_APP_PRIVATE_KEY_CONTENT_{role_u} "
            f"(inline PEM) or GITHUB_APP_PRIVATE_KEY_{role_u} (path to .pem file)"
        )
    return load_pem_private_key(Path(key_path).read_bytes(), password=None)


def _sign_jwt(app_id: str, private_key) -> str:
    """Sign a 10-minute GitHub App JWT."""
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": app_id},
        private_key,
        algorithm="RS256",
    )


def _auth_headers(jwt_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _token_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _discover_installation_id(jwt_token: str, target_repo: str) -> str:
    """Find the installation ID for target_repo (owner/repo) using the App JWT."""
    if not target_repo or "/" not in target_repo:
        raise RuntimeError(
            "Cannot discover installation ID: set GITHUB_APP_INSTALLATION_ID_{ROLE} "
            "or TARGET_REPO (owner/repo) environment variable."
        )
    owner, repo = target_repo.split("/", 1)
    resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/installation",
        headers=_auth_headers(jwt_token),
        timeout=20,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_token(
    role: str,
    env_file: Optional[Path] = None,
    target_repo: Optional[str] = None,
) -> str:
    """Mint and return a short-lived installation token for the given role.

    Args:
        role:        Agent role name (builder, reviewer, watcher, board, …).
                     Case-insensitive.
        env_file:    Path to a .env file to load before resolving credentials.
                     Defaults to .env in the current working directory.
        target_repo: "owner/repo" used to discover the installation ID if
                     GITHUB_APP_INSTALLATION_ID_{ROLE} is not set.
                     Falls back to TARGET_REPO env var.

    Returns:
        A short-lived GitHub installation token string (ghs_…).

    Raises:
        KeyError:    If required credential env vars are missing.
        RuntimeError: If installation ID cannot be discovered.
        requests.HTTPError: On GitHub API errors.
    """
    # Load .env before resolving anything.
    env_path = env_file or Path.cwd() / ".env"
    load_env(env_path)

    role_u = role.upper()
    app_id = os.environ[f"GITHUB_APP_ID_{role_u}"]
    private_key = _load_private_key(role)
    jwt_token = _sign_jwt(app_id, private_key)

    # Resolve installation ID.
    install_id = os.environ.get(f"GITHUB_APP_INSTALLATION_ID_{role_u}")
    if not install_id:
        repo = target_repo or os.environ.get("TARGET_REPO", "")
        install_id = _discover_installation_id(jwt_token, repo)

    resp = requests.post(
        f"{GITHUB_API}/app/installations/{install_id}/access_tokens",
        headers=_auth_headers(jwt_token),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def get_token_headers(role: str, **kwargs) -> dict[str, str]:
    """Convenience: mint a token and return ready-to-use request headers."""
    return _token_headers(get_token(role, **kwargs))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 -m bootstrap.github_token <role>", file=sys.stderr)
        sys.exit(1)

    role_arg = sys.argv[1].lower()
    env_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    try:
        print(get_token(role_arg, env_file=env_arg))
    except KeyError as exc:
        print(f"Missing env var: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
