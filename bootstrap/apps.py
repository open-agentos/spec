"""
bootstrap/apps.py — GitHub App registration wizard (guided manual flow).

Guides the user through creating GitHub Apps manually for each agent role
defined in agentOS.yaml that has create_app: true. Role names, permission
scopes, and event subscriptions are driven from the spec — no hardcoded
role names.

For each role the wizard:
  1. Prints the direct URL to github.com/organizations/{org}/settings/apps/new
     (or the personal-account equivalent).
  2. Prints a table of exact settings to fill in (name, permissions, events).
  3. Prompts the user to paste their new App ID and the path to the downloaded
     .pem private-key file.
  4. Calls write_credentials() to store {ROLE}_APP_ID and {ROLE}_PRIVATE_KEY
     (inline PEM with escaped newlines) in the .env file.

This flow works without a browser automation layer, local HTTP server, or
OAuth callback — making it reliable across headless environments, CI runners,
and remote SSH sessions.

Public API:
  register_apps(spec, env_file, org=None, roles=None, app_name_prefix="agentOS",
                prompt_fn=None) -> dict[str, dict]
  write_credentials(role, creds, env_file)

Requires: requests (only for the --verify flag in the CLI entry point)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

GITHUB_APP_SETTINGS_URL = "https://github.com/settings/apps/new"
GITHUB_ORG_APP_SETTINGS_URL = "https://github.com/organizations/{org}/settings/apps/new"

# Map spec permission key names to GitHub's display names (for the printed table).
_PERM_DISPLAY: dict[str, str] = {
    "contents": "Repository contents",
    "issues": "Issues",
    "pull_requests": "Pull requests",
    "metadata": "Metadata",
    "workflows": "Actions workflows",
    "checks": "Checks",
    "organization_projects": "Organization projects",
}

# Which GitHub webhook events each role should subscribe to.
_DEFAULT_ROLE_EVENTS: dict[str, list[str]] = {
    "builder": ["issues", "pull_request"],
    "reviewer": ["pull_request", "pull_request_review"],
    "watcher": ["issues"],
    "board": ["issues"],
    "docs": ["issues", "pull_request"],
    "planner": ["issues"],
}


# ---------------------------------------------------------------------------
# Credential writing
# ---------------------------------------------------------------------------

def write_credentials(role: str, creds: dict, env_file: Path) -> None:
    """Write/refresh role credentials in env_file.

    Stores PEM inline ({ROLE}_PRIVATE_KEY) with escaped newlines so it is
    safe for GHA secrets and .env files alike. Keys follow the convention
    used in the workflow templates: {ROLE}_APP_ID and {ROLE}_PRIVATE_KEY
    (e.g. BUILDER_APP_ID, BUILDER_PRIVATE_KEY).

    Note: the GITHUB_ prefix is reserved by GitHub Actions and silently
    rejected for repository secrets, so we never use it here.
    """
    role_u = role.upper()
    pem_escaped = creds["pem"].replace("\n", "\\n")
    new_entries: dict[str, str] = {
        f"{role_u}_APP_ID": creds["id"],
        f"{role_u}_PRIVATE_KEY": pem_escaped,
    }
    if creds.get("webhook_secret"):
        new_entries[f"{role_u}_WEBHOOK_SECRET"] = creds["webhook_secret"]

    existing_lines = (
        env_file.read_text(encoding="utf-8").splitlines()
        if env_file.exists() else []
    )
    out: list[str] = []
    seen: set[str] = set()

    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in new_entries:
            out.append(f"{key}={new_entries[key]}")
            seen.add(key)
        else:
            out.append(line)

    if out and out[-1].strip():
        out.append("")
    out.append(f"# --- {creds.get('slug', role)} ({role}) — auto-written by agentOS setup ---")
    for key, value in new_entries.items():
        if key not in seen:
            out.append(f"{key}={value}")
    out.append("")

    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    log.info("Wrote credentials for role '%s' (app id %s) -> %s", role, creds["id"], env_file)


# ---------------------------------------------------------------------------
# Guided manual flow
# ---------------------------------------------------------------------------

def _permissions_table(agent: dict[str, Any]) -> str:
    """Return a formatted permissions table for the printed instructions."""
    raw = agent.get("permissions", {})
    lines = []
    for key, value in raw.items():
        display = _PERM_DISPLAY.get(key, key)
        lines.append(f"    {display:<35} {value}")
    return "\n".join(lines) if lines else "    (none)"


def _print_app_instructions(
    agent: dict[str, Any],
    app_name: str,
    org: Optional[str],
) -> None:
    """Print the settings the user needs to fill in on GitHub's form."""
    role_id = agent["id"]
    events = _DEFAULT_ROLE_EVENTS.get(role_id, ["issues"])

    if org:
        settings_url = GITHUB_ORG_APP_SETTINGS_URL.format(org=org)
    else:
        settings_url = GITHUB_APP_SETTINGS_URL

    print(f"\n{'='*60}")
    print(f"  App {role_id.upper()}")
    print(f"{'='*60}")
    print(f"\n1. Open this URL in your browser:\n")
    print(f"     {settings_url}\n")
    print(f"2. Fill in the form with these settings:\n")
    print(f"   GitHub App name:  {app_name}")
    print(f"   Homepage URL:     https://github.com/open-agentos/agentos")
    print(f"   Webhooks:         ☐ Active  (uncheck — webhooks not used)")
    print(f"\n   Permissions:")
    print(_permissions_table(agent))
    print(f"\n   Subscribe to events (optional / informational):")
    for ev in events:
        print(f"    • {ev}")
    print(f"\n   Where can this GitHub App be installed?")
    print(f"    ● Only on this account")
    print(f"\n3. Click 'Create GitHub App'.")
    print(f"\n4. On the App page that opens, note the App ID shown near the top.")
    print(f"\n5. Scroll down to 'Private keys' and click 'Generate a private key'.")
    print(f"   A .pem file will download to your machine.\n")


def _prompt_credentials(
    role: str,
    app_name: str,
    prompt_fn: Callable[[str], str],
) -> dict:
    """Prompt the user for App ID and PEM file path; return a creds dict."""
    print(f"Enter credentials for '{app_name}':")

    while True:
        app_id = prompt_fn(f"  App ID (numbers only): ").strip()
        if app_id.isdigit():
            break
        print("  ✗ App ID must be numeric — check the App page and try again.")

    while True:
        pem_path_str = prompt_fn(f"  Path to downloaded .pem file: ").strip()
        pem_path = Path(pem_path_str).expanduser()
        if pem_path.exists():
            pem_content = pem_path.read_text(encoding="utf-8").strip()
            if "BEGIN RSA PRIVATE KEY" in pem_content or "BEGIN PRIVATE KEY" in pem_content:
                break
            print(f"  ✗ File does not look like a PEM private key — try again.")
        else:
            print(f"  ✗ File not found: {pem_path} — try again.")

    return {
        "id": app_id,
        "slug": app_name,
        "pem": pem_content + "\n",
        "webhook_secret": "",
        "html_url": f"https://github.com/apps/{app_name}",
    }


# ---------------------------------------------------------------------------
# Public orchestration function
# ---------------------------------------------------------------------------

def register_apps(
    spec: dict[str, Any],
    env_file: Path,
    org: Optional[str] = None,
    roles: Optional[list[str]] = None,
    app_name_prefix: str = "agentOS",
    prompt_fn: Optional[Callable[[str], str]] = None,
    # Legacy keyword args accepted but ignored (port, open_browser removed)
    **_kwargs: Any,
) -> dict[str, dict]:
    """Guided manual GitHub App registration for all create_app:true roles.

    Prints step-by-step instructions for each role and prompts the user for
    the App ID and private key path. Writes credentials to env_file.

    Args:
        spec:             Parsed agentOS.yaml dict.
        env_file:         Path to write credentials to.
        org:              GitHub org name (None = personal account).
        roles:            Limit to these role IDs. None = all create_app:true roles.
        app_name_prefix:  Prefix for app names (e.g. "agentOS" -> "agentOS-builder").
        prompt_fn:        Callable used to prompt the user. Defaults to input().
                          Override in tests to avoid interactive prompts.

    Returns:
        Dict of {role_id: credentials_dict} for all successfully registered roles.
    """
    if prompt_fn is None:
        prompt_fn = input

    agents = spec.get("agents", [])
    to_register = [
        a for a in agents
        if a.get("create_app", False)
        and (roles is None or a["id"] in roles)
    ]

    if not to_register:
        log.warning("No agents with create_app:true found in spec.")
        return {}

    target_desc = f"org: {org}" if org else "personal account"
    print(f"\nagentOS setup — {len(to_register)} GitHub App(s) to create")
    print(f"  target:   {target_desc}")
    print(f"  env file: {env_file}")
    if not org:
        print(
            "\n  ⚠ Warning: creating Apps under your personal account.\n"
            "    Pass --org <org> (or --repo org/repo) to create org-level Apps.",
            file=sys.stderr,
        )
    print(
        "\nFor each role you will:\n"
        "  • Open a GitHub URL and fill in a short form\n"
        "  • Paste the App ID back here\n"
        "  • Provide the path to the downloaded .pem private key\n"
        "\nPress Ctrl-C at any time to abort."
    )

    results: dict[str, dict] = {}
    for agent in to_register:
        role_id = agent["id"]
        app_name = f"{app_name_prefix}-{role_id}"

        _print_app_instructions(agent, app_name, org)

        try:
            creds = _prompt_credentials(role_id, app_name, prompt_fn)
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.", file=sys.stderr)
            break
        except Exception as exc:
            log.error("Failed to collect credentials for '%s': %s", role_id, exc)
            print(f"  ERROR: {role_id} — {exc}", file=sys.stderr)
            continue

        write_credentials(role_id, creds, env_file)
        print(f"  ✓ {role_id} app registered — App ID: {creds['id']}")
        role_u = role_id.upper()
        install_url = (
            f"https://github.com/organizations/{org}/settings/apps/{app_name}/installations"
            if org
            else f"https://github.com/settings/apps/{app_name}/installations"
        )
        print(f"  IMPORTANT: install the app on your target repo:\n    {install_url}")
        results[role_id] = creds

    return results


# ---------------------------------------------------------------------------
# CLI entry point (used by `agentOS setup`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yaml

    parser = argparse.ArgumentParser(
        description="Register GitHub Apps via guided manual flow."
    )
    parser.add_argument("--spec", default="agentOS.yaml", help="Path to agentOS.yaml")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--org", help="GitHub org (default: personal account)")
    parser.add_argument("--prefix", default="agentOS", help="App name prefix")
    parser.add_argument(
        "--role", action="append", dest="roles",
        help="Register only this role (repeatable)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"Spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)

    with open(spec_path) as f:
        spec_data = yaml.safe_load(f)

    results = register_apps(
        spec=spec_data,
        env_file=Path(args.env),
        org=args.org,
        roles=args.roles,
        app_name_prefix=args.prefix,
    )

    print(f"\nDone. {len(results)} app(s) registered.")
    print("Next: run  agentOS apply --repo owner/my-repo")
