"""
bootstrap/apps.py — GitHub App registration wizard.

Registers GitHub Apps via the App Manifest flow for each agent role defined
in agentOS.yaml that has create_app: true. Role names, permission scopes, and
event subscriptions are all driven from the spec — no hardcoded role names.

For each role:
  1. Spins up a temporary local HTTP server (default http://localhost:4000).
  2. Opens the GitHub App manifest form in the browser.
  3. Receives the OAuth callback code after the user confirms on GitHub.
  4. Exchanges the code for credentials via POST /app-manifests/{code}/conversions.
  5. Writes GITHUB_APP_ID_{ROLE} and GITHUB_APP_PRIVATE_KEY_CONTENT_{ROLE}
     (and optionally GITHUB_APP_WEBHOOK_SECRET_{ROLE}) into the .env file.

Public API:
  register_apps(spec, env_file, org=None, port=4000, open_browser=True, roles=None)
  write_credentials(role, creds, env_file)

Requires: requests
    uv run --with requests python3 -m bootstrap.apps
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Map spec permission key names to GitHub App manifest permission keys.
# The spec uses snake_case; GitHub's manifest API uses snake_case too —
# but "organization_projects" must be listed as "organization_projects".
_PERM_KEY_MAP = {
    "contents": "contents",
    "issues": "issues",
    "pull_requests": "pull_requests",
    "metadata": "metadata",
    "workflows": "workflows",
    "checks": "checks",
    "organization_projects": "organization_projects",
}

# Which GitHub webhook events each role should subscribe to.
# Driven by the routing triggers in the spec, but event subscriptions are
# a GitHub concept not directly expressed in agentOS.yaml.
_DEFAULT_ROLE_EVENTS: dict[str, list[str]] = {
    "builder": ["issues", "pull_request"],
    "reviewer": ["pull_request", "pull_request_review"],
    "watcher": ["issues"],
    "board": ["issues"],
    "docs": ["issues", "pull_request"],
    "planner": ["issues"],
}


# ---------------------------------------------------------------------------
# Manifest construction
# ---------------------------------------------------------------------------

def _permissions_for_role(agent: dict[str, Any]) -> dict[str, str]:
    """Build a GitHub manifest permissions dict from an agent spec entry."""
    raw = agent.get("permissions", {})
    result: dict[str, str] = {}
    for key, value in raw.items():
        mapped = _PERM_KEY_MAP.get(key, key)
        result[mapped] = value  # type: ignore[assignment]  # mapped is always str
    return result


def build_manifest(agent: dict[str, Any], app_name: str, base_url: str) -> dict:
    """Construct a GitHub App manifest for the given agent spec entry."""
    role_id = agent["id"]
    permissions = _permissions_for_role(agent)
    events = _DEFAULT_ROLE_EVENTS.get(role_id, ["issues"])
    import os
    app_url = os.environ.get(
        "AGENTOS_APP_URL", "https://github.com/open-agentos/spec"
    )
    if not app_url.startswith("https://"):
        raise ValueError(
            f"AGENTOS_APP_URL must be an https:// URL (got {app_url!r}). "
            "GitHub rejects http:// in the App manifest url field."
        )
    return {
        "name": app_name,
        "url": app_url,
        "hook_attributes": {"active": False},
        "redirect_url": f"{base_url}/callback",
        "public": False,
        "default_permissions": permissions,
        "default_events": events,
    }


# ---------------------------------------------------------------------------
# HTTP server for manifest OAuth flow
# ---------------------------------------------------------------------------

def _manifest_form_page(action_url: str, manifest: dict) -> bytes:
    manifest_json = html.escape(json.dumps(manifest))
    app_name = html.escape(manifest["name"])
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Registering {app_name}…</title></head>
<body style="font-family:system-ui;margin:3rem">
  <h2>Creating GitHub App: {app_name}</h2>
  <p>Submitting manifest to GitHub. If you are not redirected automatically,
     click the button below.</p>
  <form id="f" action="{html.escape(action_url)}" method="post">
    <input type="hidden" name="manifest" value="{manifest_json}">
    <button type="submit">Continue to GitHub →</button>
  </form>
  <script>document.getElementById('f').submit();</script>
</body></html>""".encode("utf-8")


def _success_page(app_slug: str) -> bytes:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>App created</title></head>
<body style="font-family:system-ui;margin:3rem">
  <h2>✓ GitHub App created: {html.escape(app_slug)}</h2>
  <p>Credentials written to your .env file. You can close this tab.</p>
</body></html>""".encode("utf-8")


class _CallbackState:
    code: Optional[str] = None
    error: Optional[str] = None
    done = threading.Event()


def _make_handler(manifest: dict, action_url: str, state: _CallbackState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default stderr log
            # Signature matches BaseHTTPRequestHandler.log_message(format, *args)
            return

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/start"):
                body = _manifest_form_page(action_url, manifest)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/callback":
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                if code:
                    state.code = code
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(_success_page(manifest["name"]))
                else:
                    state.error = "no code in callback"
                    self.send_response(400)
                    self.end_headers()
                state.done.set()
                return
            self.send_response(404)
            self.end_headers()
    return Handler


# ---------------------------------------------------------------------------
# Registration + credential writing
# ---------------------------------------------------------------------------

def _register_one(agent: dict[str, Any], app_name: str, org: Optional[str],
                  port: int, open_browser: bool) -> dict:
    """Run the manifest OAuth flow for one agent role. Returns credential dict."""
    base_url = f"http://localhost:{port}"
    manifest = build_manifest(agent, app_name, base_url)
    role_id = agent["id"]

    if org:
        action_url = f"https://github.com/organizations/{org}/settings/apps/new?state=agentOS-{role_id}"
    else:
        action_url = f"https://github.com/settings/apps/new?state=agentOS-{role_id}"

    state = _CallbackState()
    handler = _make_handler(manifest, action_url, state)
    server = HTTPServer(("localhost", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    start_url = f"{base_url}/start"
    print(f"\n=== {role_id.upper()} app ===")
    print(f"Open this URL to register '{app_name}':\n  {start_url}")
    if open_browser:
        try:
            webbrowser.open(start_url)
        except Exception:
            pass

    print("Waiting for GitHub callback (Ctrl-C to abort)…")
    state.done.wait()
    server.shutdown()

    if state.error or not state.code:
        raise RuntimeError(f"Manifest callback failed: {state.error or 'no code received'}")

    resp = requests.post(
        f"https://api.github.com/app-manifests/{state.code}/conversions",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "id": str(data["id"]),
        "slug": data.get("slug", app_name),
        "pem": data["pem"],
        "webhook_secret": data.get("webhook_secret", ""),
        "html_url": data.get("html_url", ""),
    }


def write_credentials(role: str, creds: dict, env_file: Path) -> None:
    """Write/refresh role credentials in env_file.

    Stores PEM inline (GITHUB_APP_PRIVATE_KEY_CONTENT_{ROLE}) with escaped
    newlines so it is safe for GHA secrets and .env files alike. Never writes
    the PEM as a separate file on disk.
    """
    role_u = role.upper()
    pem_escaped = creds["pem"].replace("\n", "\\n")
    new_entries: dict[str, str] = {
        f"GITHUB_APP_ID_{role_u}": creds["id"],
        f"GITHUB_APP_PRIVATE_KEY_CONTENT_{role_u}": pem_escaped,
    }
    if creds.get("webhook_secret"):
        new_entries[f"GITHUB_APP_WEBHOOK_SECRET_{role_u}"] = creds["webhook_secret"]

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
    out.append(f"# --- {creds['slug']} ({role}) — auto-written by agentOS setup ---")
    for key, value in new_entries.items():
        if key not in seen:
            out.append(f"{key}={value}")
    out.append("")

    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    log.info("Wrote credentials for role '%s' (app id %s) -> %s", role, creds["id"], env_file)


# ---------------------------------------------------------------------------
# Public orchestration function
# ---------------------------------------------------------------------------

def register_apps(
    spec: dict[str, Any],
    env_file: Path,
    org: Optional[str] = None,
    port: int = 4000,
    open_browser: bool = True,
    roles: Optional[list[str]] = None,
    app_name_prefix: str = "agentOS",
) -> dict[str, dict]:
    """Register GitHub Apps for all create_app:true roles in the spec.

    Args:
        spec:             Parsed agentOS.yaml dict.
        env_file:         Path to write credentials to.
        org:              GitHub org name (None = personal account).
        port:             Local callback port.
        open_browser:     Auto-open browser for each registration.
        roles:            Limit to these role IDs. None = all create_app:true roles.
        app_name_prefix:  Prefix for app names (e.g. "agentOS" -> "agentOS-builder").

    Returns:
        Dict of {role_id: credentials_dict} for all successfully registered roles.
    """
    agents = spec.get("agents", [])
    to_register = [
        a for a in agents
        if a.get("create_app", False)
        and (roles is None or a["id"] in roles)
    ]

    if not to_register:
        log.warning("No agents with create_app:true found in spec.")
        return {}

    print(f"GitHub App registration wizard — {len(to_register)} role(s) to register")
    print(f"  target: {'org ' + org if org else 'personal account'}")
    print(f"  env file: {env_file}")

    results: dict[str, dict] = {}
    for agent in to_register:
        role_id = agent["id"]
        app_name = f"{app_name_prefix}-{role_id}"
        try:
            creds = _register_one(agent, app_name, org, port, open_browser)
        except Exception as exc:
            log.error("Failed to register '%s': %s", role_id, exc)
            print(f"  ERROR: {role_id} registration failed — {exc}", file=sys.stderr)
            continue

        write_credentials(role_id, creds, env_file)
        print(f"  {role_id}: {creds['html_url']}")
        print(f"  IMPORTANT: install the app on your target repo from the URL above.")
        results[role_id] = creds

    return results


# ---------------------------------------------------------------------------
# CLI entry point (used by `agentOS setup`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yaml

    parser = argparse.ArgumentParser(description="Register GitHub Apps via manifest flow.")
    parser.add_argument("--spec", default="agentOS.yaml", help="Path to agentOS.yaml")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--org", help="GitHub org (default: personal account)")
    parser.add_argument("--prefix", default="agentOS", help="App name prefix")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--role", action="append", dest="roles",
                        help="Register only this role (repeatable)")
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
        port=args.port,
        open_browser=not args.no_browser,
        roles=args.roles,
        app_name_prefix=args.prefix,
    )

    print(f"\nDone. {len(results)} app(s) registered.")
    print("Next: run  agentOS apply --repo owner/my-repo")
