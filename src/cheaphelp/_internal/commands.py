"""Implementations of the cheaphelp CLI subcommands.

Each `cmd_*` function takes the parsed argparse namespace and returns a process
exit code. Keeping them here keeps `cli.py` focused on argument wiring.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from cheaphelp._internal import opencode, systemd
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.env import (
    GITHUB_TOKEN_KEY,
    OPENROUTER_API_KEY,
    load_into_environ,
    read_env_file,
    update_env_file,
)
from cheaphelp._internal.github import GitHubClient, GitHubError
from cheaphelp._internal.orchestrator import tick
from cheaphelp._internal.registry import Registry, RepoEntry, parse_slug


def _workspace(args: argparse.Namespace) -> Workspace:
    return Workspace(Path(args.home).expanduser() if getattr(args, "home", None) else None)


def _require_workspace(ws: Workspace) -> int | None:
    if not ws.exists():
        print(f"No workspace at {ws.home}. Run `cheaphelp init` first.", file=sys.stderr)
        return 1
    return None


# --- init ------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    fresh = not ws.exists()
    ws.ensure()
    print(f"Workspace: {ws.home}")

    # Config: keep existing, create defaults if missing.
    config = ws.load_config()
    if not ws.config_path.exists():
        ws.save_config(config)
        print(f"  created {ws.config_path.name} (cheap OpenRouter test models)")

    # Secrets: from flags, else interactive prompt (unless --no-prompt).
    updates: dict[str, str] = {}
    if args.github_token:
        updates[GITHUB_TOKEN_KEY] = args.github_token
    if args.openrouter_key:
        updates[OPENROUTER_API_KEY] = args.openrouter_key

    existing = read_env_file(ws.env_path)
    if not args.no_prompt and sys.stdin.isatty():
        if GITHUB_TOKEN_KEY not in updates and not existing.get(GITHUB_TOKEN_KEY):
            entered = getpass.getpass("GitHub personal access token (blank to skip): ").strip()
            if entered:
                updates[GITHUB_TOKEN_KEY] = entered
        if OPENROUTER_API_KEY not in updates and not existing.get(OPENROUTER_API_KEY):
            entered = getpass.getpass("OpenRouter API key (blank to skip): ").strip()
            if entered:
                updates[OPENROUTER_API_KEY] = entered

    update_env_file(ws.env_path, updates)
    print(f"  secrets file: {ws.env_path} (chmod 600)")

    # Agent prompts + opencode config.
    written = opencode.write_workspace_prompts(ws, overwrite=False)
    if written:
        print(f"  wrote agent prompts: {', '.join(written)}")
    cfg_path = opencode.write_opencode_config(ws, config)
    print(f"  generated {cfg_path}")

    # opencode availability check.
    if opencode.find_opencode(config) is None:
        print(f"\n  ! opencode not found (looked for {config.opencode_bin!r} on PATH).")
        print("    Install it: curl -fsSL https://opencode.ai/install | bash")
        print("    or: npm i -g opencode-ai")

    print("\nNext steps:")
    print("  1. Ensure your tokens are set:    cheaphelp doctor")
    print("  2. Register a repo:               cheaphelp repo add owner/name")
    print("  3. Try a dry run:                 cheaphelp run --once --dry-run")
    print("  4. Install the timer:             cheaphelp systemd install --interval 10m")
    if not fresh:
        print("\n(Existing workspace updated; nothing was overwritten.)")
    return 0


# --- repo ------------------------------------------------------------------
def cmd_repo_add(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    try:
        owner, name = parse_slug(args.slug)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    default_branch = "main"
    load_into_environ(ws.env_path)
    token = os.environ.get(GITHUB_TOKEN_KEY, "")
    if token:
        try:
            with GitHubClient(token) as gh:
                meta = gh.get_repo(owner, name)
                default_branch = meta.get("default_branch", "main")
                print(f"Verified {owner}/{name} (default branch: {default_branch}).")
        except GitHubError as exc:
            print(f"Warning: could not verify repo via API: {exc}", file=sys.stderr)
    else:
        print("Warning: no GITHUB_TOKEN set; adding without verification.", file=sys.stderr)

    registry = Registry(ws.registry_path)
    added = registry.add(
        RepoEntry(
            owner=owner,
            name=name,
            default_branch=default_branch,
            autofix=getattr(args, "autofix", "") or "",
            checks=getattr(args, "checks", "") or "",
        ),
    )
    if not added:
        print(f"{owner}/{name} is already registered.")
        return 0
    print(f"Registered {owner}/{name}.")
    return 0


def cmd_repo_list(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    repos = Registry(ws.registry_path).load()
    if getattr(args, "json", False):
        payload = [
            {
                **asdict(repo),
                "slug": repo.slug,
            }
            for repo in repos
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not repos:
        print("No repositories registered. Add one with `cheaphelp repo add owner/name`.")
        return 0
    for repo in repos:
        flag = "on " if repo.enabled else "off"
        print(f"  [{flag}] {repo.slug}  (branch: {repo.default_branch})")
    return 0


def cmd_repo_remove(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    owner, name = parse_slug(args.slug)
    if Registry(ws.registry_path).remove(owner, name):
        print(f"Removed {owner}/{name}.")
        return 0
    print(f"{owner}/{name} was not registered.", file=sys.stderr)
    return 1


def cmd_repo_toggle(args: argparse.Namespace, *, enabled: bool) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    owner, name = parse_slug(args.slug)
    if Registry(ws.registry_path).set_enabled(owner, name, enabled):
        print(f"{'Enabled' if enabled else 'Disabled'} {owner}/{name}.")
        return 0
    print(f"{owner}/{name} was not registered.", file=sys.stderr)
    return 1


# --- run -------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    report = tick(ws, dry_run=args.dry_run, log=print)
    if report.error:
        print(f"\nError: {report.error}", file=sys.stderr)
        return 1
    print(f"\nDone. {report.total_turns} agent turn(s) across {len(report.repos)} repo(s).")
    return 0


# --- systemd ---------------------------------------------------------------
def cmd_systemd_install(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    try:
        actions = systemd.install(home=ws.home, interval=args.interval)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for action in actions:
        print(f"  {action}")
    print("\nTip: for the timer to run when you are logged out:")
    print("  loginctl enable-linger $USER")
    return 0


def cmd_systemd_uninstall(args: argparse.Namespace) -> int:
    for action in systemd.uninstall():
        print(f"  {action}")
    return 0


def cmd_systemd_status(args: argparse.Namespace) -> int:
    print(systemd.status())
    return 0


# --- agents ----------------------------------------------------------------
def cmd_agents_sync(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    written = opencode.write_workspace_prompts(ws, overwrite=args.force)
    config = ws.load_config()
    path = opencode.write_opencode_config(ws, config)
    if written:
        print(f"Wrote prompts: {', '.join(written)}")
    elif args.force:
        print("Overwrote all bundled prompts.")
    else:
        print("Kept existing prompts (use --force to overwrite from bundled).")
    print(f"Regenerated {path}")
    return 0


# --- doctor ----------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "OK " if passed else "FAIL"
        ok = ok and passed
        print(f"  [{mark}] {label}{(' - ' + detail) if detail else ''}")

    print(f"Workspace: {ws.home}")
    check("workspace initialised", ws.exists(), "" if ws.exists() else "run `cheaphelp init`")
    if not ws.exists():
        return 1

    config = ws.load_config()
    env = read_env_file(ws.env_path)
    check(f"{GITHUB_TOKEN_KEY} present", bool(env.get(GITHUB_TOKEN_KEY)))
    check(f"{OPENROUTER_API_KEY} present", bool(env.get(OPENROUTER_API_KEY)))

    bin_path = opencode.find_opencode(config)
    check("opencode on PATH", bin_path is not None, bin_path or "install from https://opencode.ai")

    token = env.get(GITHUB_TOKEN_KEY, "")
    if token:
        try:
            with GitHubClient(token) as gh:
                login = gh.authenticated_login()
            check("GitHub token valid", True, f"authenticated as @{login}")
        except GitHubError as exc:
            check("GitHub token valid", False, str(exc))
    else:
        check("GitHub token valid", False, "no token to test")

    repos = Registry(ws.registry_path).load()
    check("repositories registered", bool(repos), f"{len(repos)} registered")

    print("\nModels:")
    for role, model in config.models.items():
        print(f"  {role:<9} {model}")

    return 0 if ok else 1


def add_config_overrides(config: Config) -> None:  # pragma: no cover - reserved
    """Placeholder for future per-invocation config overrides."""
