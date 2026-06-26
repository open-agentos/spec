"""
bootstrap/cli.py — agentOS CLI entry point.

Subcommands:
  agentOS init   [--from source]           Generate agentOS.yaml
  agentOS setup  --repo owner/repo         Register GitHub Apps (interactive)
  agentOS apply  --repo owner/repo [flags] Provision labels, board, workflows
  agentOS verify --repo owner/repo         Health check against agentOS.yaml
  agentOS token  <role>                    Print a short-lived App token

Global flags:
  --spec PATH    Path to agentOS.yaml (default: ./agentOS.yaml)
  --env PATH     Path to .env file (default: ./.env)
  --log LEVEL    Logging level: DEBUG|INFO|WARNING (default: INFO)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from bootstrap import __version__

log = logging.getLogger("agentOS")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_spec(spec_path: Path) -> dict:
    if not spec_path.exists():
        print(f"error: spec file not found: {spec_path}", file=sys.stderr)
        print("  Run `agentOS init` to generate one.", file=sys.stderr)
        sys.exit(1)
    with open(spec_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_env(env_path: Path) -> None:
    from bootstrap.github_token import load_env
    load_env(env_path)


def _resolve_token(env_var: str) -> Optional[str]:
    return os.environ.get(env_var)


def _require_token(env_var: str, hint: str) -> str:
    token = _resolve_token(env_var)
    if not token:
        print(f"error: {env_var} not set.", file=sys.stderr)
        print(f"  {hint}", file=sys.stderr)
        sys.exit(1)
    return token


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    """Generate agentOS.yaml — from a remote source or as a blank template."""
    dest = Path(args.output or "agentOS.yaml")
    if dest.exists() and not args.force:
        print(f"error: {dest} already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    if args.source:
        print(f"Fetching spec from {args.source} …")
        # Source format: github:owner/repo//path@ref  or  local:path
        if args.source.startswith("local:"):
            src_path = Path(args.source[6:])
            content = src_path.read_text(encoding="utf-8")
        elif args.source.startswith("github:"):
            import urllib.request
            # Supported formats:
            #   github:owner/repo@ref               -> agentOS.yaml at ref
            #   github:owner/repo//path/to/file@ref -> specific file at ref
            raw = args.source[7:]   # strip "github:"
            if "//" in raw:
                repo_part, rest = raw.split("//", 1)
            else:
                repo_part, rest = raw, "agentOS.yaml@main"
            # Split repo@ref if @ present in repo_part (short form)
            if "@" in repo_part:
                repo, inferred_ref = repo_part.rsplit("@", 1)
                # If rest still has @, honour it; otherwise use inferred_ref
                if "@" not in rest:
                    rest = f"{rest}@{inferred_ref}"
            else:
                repo = repo_part
            if "@" in rest:
                file_path, git_ref = rest.rsplit("@", 1)
            else:
                file_path, git_ref = rest, "main"
            url = f"https://raw.githubusercontent.com/{repo}/{git_ref}/{file_path}"
            print(f"  -> {url}")
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                content = resp.read().decode("utf-8")
        else:
            print(f"error: unknown source format: {args.source}", file=sys.stderr)
            print("  Supported: local:path  or  github:owner/repo//path@ref", file=sys.stderr)
            return 1

        dest.write_text(content, encoding="utf-8")
        print(f"Wrote {dest}")
    else:
        # Copy the bundled agentOS.yaml from the spec repo itself.
        bundled = Path(__file__).resolve().parent.parent / "agentOS.yaml"
        if bundled.exists():
            content = bundled.read_text(encoding="utf-8")
        else:
            print("error: bundled agentOS.yaml not found", file=sys.stderr)
            return 1
        dest.write_text(content, encoding="utf-8")
        print(f"Generated {dest} from bundled spec.")
        print("Edit it to customise roles, labels, and board fields.")

    # Create .agentOS/ scaffold directories
    scaffold_dirs = [
        Path(".agentOS/keys"),
        Path(".agentOS/logs"),
        Path(".agentOS/plugins"),
    ]
    for d in scaffold_dirs:
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()
    print("Created .agentOS/ scaffold (keys/, logs/, plugins/)")

    # Update .gitignore — add .agentOS/keys/ if not already present
    gitignore = Path(".gitignore")
    gitignore_entries = [
        "# agentOS — local secrets and runtime state",
        ".agentOS/keys/",
        ".agentOS/logs/",
        ".agentOS-state.json",
    ]
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    additions = [e for e in gitignore_entries if e not in existing]
    if additions:
        with gitignore.open("a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(additions) + "\n")
        print(f"Updated .gitignore with {len(additions)} agentOS entries")

    return 0


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive GitHub App registration wizard."""
    spec = _load_spec(Path(args.spec))
    _load_env(Path(args.env))

    from bootstrap.apps import register_apps
    results = register_apps(
        spec=spec,
        env_file=Path(args.env),
        org=args.org,
        port=args.port,
        open_browser=not args.no_browser,
        roles=args.role or None,
        app_name_prefix=args.prefix,
    )

    if not results:
        print("No apps registered.", file=sys.stderr)
        return 1

    print(f"\nRegistered {len(results)} app(s).")
    print("Next steps:")
    for role_id, creds in results.items():
        print(f"  1. Install the {role_id} app on your repo: {creds['html_url']}")
    print(f"  2. Run:  agentOS apply --repo {args.repo}")
    return 0


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def cmd_apply(args: argparse.Namespace) -> int:
    """Provision labels, board, and workflows to a target repo."""
    spec = _load_spec(Path(args.spec))
    _load_env(Path(args.env))

    # Resolve tokens.
    labels_token = _require_token(
        "GITHUB_TOKEN",
        "Set GITHUB_TOKEN to a token with issues:write and metadata:read scopes.",
    )
    board_token: Optional[str] = None
    if not args.skip or "board" not in args.skip:
        # Try board role token first, fall back to GITHUB_TOKEN.
        try:
            from bootstrap.github_token import get_token
            board_token = get_token("board", env_file=Path(args.env),
                                    target_repo=args.repo)
            log.debug("Using board App token for Projects v2")
        except Exception:
            board_token = labels_token
            log.debug("No board App credentials — using GITHUB_TOKEN for board")

    from bootstrap.apply import ApplyOptions, apply
    opts = ApplyOptions(
        repo=args.repo,
        labels_token=labels_token,
        board_token=board_token,
        org=args.org,
        target_dir=Path(args.target_dir) if args.target_dir else None,
        force_workflows=args.force,
        dry_run=args.dry_run,
        reset=args.reset,
        only=args.only or None,
        skip=args.skip or None,
    )

    result = apply(spec, opts)
    result.print_summary()
    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    """Verify target repo matches agentOS.yaml."""
    spec = _load_spec(Path(args.spec))
    _load_env(Path(args.env))

    token = _require_token(
        "GITHUB_TOKEN",
        "Set GITHUB_TOKEN to a token with issues:read and metadata:read scopes.",
    )
    board_token: Optional[str] = None
    try:
        from bootstrap.github_token import get_token
        board_token = get_token("board", env_file=Path(args.env), target_repo=args.repo)
    except Exception:
        board_token = None

    from bootstrap.verify import verify
    result = verify(
        spec=spec,
        repo=args.repo,
        token=token,
        bindings_path=Path(args.bindings) if args.bindings else None,
        board_token=board_token,
    )
    result.print_report()
    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------

def cmd_token(args: argparse.Namespace) -> int:
    """Print a short-lived GitHub App installation token for a role."""
    _load_env(Path(args.env))
    try:
        from bootstrap.github_token import get_token
        token = get_token(args.role, env_file=Path(args.env), target_repo=args.repo)
        print(token)
        return 0
    except KeyError as exc:
        print(f"error: missing credential env var: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentOS",
        description="GitHub AgentOS — provision label-driven multi-agent orchestration.",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"agentOS {__version__}",
    )
    parser.add_argument("--spec", default="agentOS.yaml",
                        help="Path to agentOS.yaml (default: ./agentOS.yaml)")
    parser.add_argument("--env", default=".env",
                        help="Path to .env file (default: ./.env)")
    parser.add_argument("--log", default="INFO", dest="log_level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level (default: INFO)")

    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Generate agentOS.yaml")
    p_init.add_argument("--from", dest="source", metavar="SOURCE",
                        help="Source spec: github:owner/repo//path@ref or local:path")
    p_init.add_argument("--output", "-o", help="Output path (default: ./agentOS.yaml)")
    p_init.add_argument("--force", action="store_true",
                        help="Overwrite existing agentOS.yaml")

    # setup
    p_setup = sub.add_parser("setup", help="Register GitHub Apps (interactive wizard)")
    p_setup.add_argument("--repo", required=True, metavar="OWNER/REPO")
    p_setup.add_argument("--org", help="GitHub org for app creation")
    p_setup.add_argument("--prefix", default="agentOS", help="App name prefix")
    p_setup.add_argument("--port", type=int, default=4000)
    p_setup.add_argument("--no-browser", action="store_true")
    p_setup.add_argument("--role", action="append",
                         help="Register only this role (repeatable)")

    # apply
    p_apply = sub.add_parser("apply", help="Provision labels, board, and workflows")
    p_apply.add_argument("--repo", required=True, metavar="OWNER/REPO")
    p_apply.add_argument("--org", help="GitHub org for Projects v2 board")
    p_apply.add_argument("--target-dir", help="Local checkout of target repo (for workflow copy)")
    p_apply.add_argument("--bindings", help="Path to field-bindings.json")
    p_apply.add_argument("--only", action="append", metavar="STEP",
                         help="Run only this step (repeatable): labels|board|workflows|scaffold")
    p_apply.add_argument("--skip", action="append", metavar="STEP",
                         help="Skip this step (repeatable)")
    p_apply.add_argument("--force", action="store_true",
                         help="Overwrite existing modified workflow files")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="Show what would happen without making changes")
    p_apply.add_argument("--reset", action="store_true",
                         help="Reset state file and re-run all steps from scratch")

    # verify
    p_verify = sub.add_parser("verify", help="Check repo matches agentOS.yaml")
    p_verify.add_argument("--repo", required=True, metavar="OWNER/REPO")
    p_verify.add_argument("--bindings", help="Path to field-bindings.json")

    # token
    p_token = sub.add_parser("token", help="Print a short-lived App installation token")
    p_token.add_argument("role", help="Agent role (builder|reviewer|watcher|board|…)")
    p_token.add_argument("--repo", metavar="OWNER/REPO",
                         help="Target repo for installation ID discovery")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)-8s %(message)s",
    )

    dispatch = {
        "init": cmd_init,
        "setup": cmd_setup,
        "apply": cmd_apply,
        "verify": cmd_verify,
        "token": cmd_token,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
