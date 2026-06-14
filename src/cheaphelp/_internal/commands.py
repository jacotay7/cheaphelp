"""Implementations of the cheaphelp CLI subcommands.

Each `cmd_*` function takes the parsed argparse namespace and returns a process
exit code. Keeping them here keeps `cli.py` focused on argument wiring.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cheaphelp._internal import cleanup, opencode, systemd
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.env import (
    GITHUB_TOKEN_KEY,
    OPENROUTER_API_KEY,
    load_into_environ,
    read_env_file,
    update_env_file,
)
from cheaphelp._internal.github import GitHubClient, GitHubError
from cheaphelp._internal.opencode import UsageData
from cheaphelp._internal.orchestrator import classify, tick
from cheaphelp._internal.registry import Registry, RepoEntry, parse_slug
from cheaphelp._internal.tasks import IssueCostStore

_LOG_TAIL_LINES = 50


def _workspace(args: argparse.Namespace) -> Workspace:
    return Workspace(Path(args.home).expanduser() if getattr(args, "home", None) else None)


def _require_workspace(ws: Workspace) -> int | None:
    if not ws.exists():
        print(f"No workspace at {ws.home}. Run `cheaphelp init` first.", file=sys.stderr)
        return 1
    return None


def _format_cost_lines(report: object) -> list[str]:
    """Return the per-tick cost summary lines (excluding the leading "Cost:").

    The first line is the aggregate; subsequent lines are per-issue with a
    per-role breakdown. Returns [] when there is no recorded cost.
    """
    total_cost: UsageData = getattr(report, "total_cost", UsageData())
    if total_cost.cost_usd == 0.0 and total_cost.total_tokens == 0:
        return []

    lines: list[str] = [
        f"Cost: ${total_cost.cost_usd:.3f} ({total_cost.prompt_tokens:,} prompt + {total_cost.completion_tokens:,} completion tokens)",
    ]

    repos: list[object] = getattr(report, "repos", [])
    for repo in repos:
        slug: str = getattr(repo, "slug", "")
        issue_costs: dict[int, dict[str, list[UsageData]]] = getattr(repo, "issue_costs", {})
        for number in sorted(issue_costs):
            by_role = issue_costs[number]
            # Compute issue_total and breakdown_str.
            role_order = ["responder", "planner", "worker", "reviewer"]
            seen: set[str] = set()
            parts: list[str] = []
            issue_total = UsageData()
            for role in role_order:
                if role in by_role:
                    seen.add(role)
                    usages = by_role[role]
                    total_for_role = sum(usages, UsageData())
                    issue_total += total_for_role
                    count = len(usages)
                    if count > 1:
                        parts.append(f"{role} x{count} ${total_for_role.cost_usd:.3f}")
                    else:
                        parts.append(f"{role} ${total_for_role.cost_usd:.3f}")
            # Remaining roles (outside the stable order).
            for role in sorted(by_role):
                if role not in seen:
                    usages = by_role[role]
                    total_for_role = sum(usages, UsageData())
                    issue_total += total_for_role
                    count = len(usages)
                    if count > 1:
                        parts.append(f"{role} x{count} ${total_for_role.cost_usd:.3f}")
                    else:
                        parts.append(f"{role} ${total_for_role.cost_usd:.3f}")
            lines.append(f"  {slug}#{number}:   ${issue_total.cost_usd:.3f}  ({', '.join(parts)})")
    return lines


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
    mdf = getattr(args, "max_diff_files", None)
    mdl = getattr(args, "max_diff_lines", None)
    added = registry.add(
        RepoEntry(
            owner=owner,
            name=name,
            default_branch=default_branch,
            autofix=getattr(args, "autofix", "") or "",
            checks=getattr(args, "checks", "") or "",
            max_diff_files=30 if mdf is None else mdf,
            max_diff_lines=1000 if mdl is None else mdl,
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


def cmd_repo_set(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc
    try:
        owner, name = parse_slug(args.slug)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    updates: dict[str, str | int] = {}
    if getattr(args, "checks", None) is not None:
        updates["checks"] = args.checks
    if getattr(args, "autofix", None) is not None:
        updates["autofix"] = args.autofix
    if getattr(args, "max_diff_files", None) is not None:
        updates["max_diff_files"] = args.max_diff_files
    if getattr(args, "max_diff_lines", None) is not None:
        updates["max_diff_lines"] = args.max_diff_lines

    if not updates:
        print(f"Nothing to update for {owner}/{name}.")
        return 0

    registry = Registry(ws.registry_path)
    if registry.find(owner, name) is None:
        print(f"{owner}/{name} is not registered.", file=sys.stderr)
        return 1
    if registry.update(owner, name, **updates):
        print(f"Updated {owner}/{name}.")
        return 0
    print(f"No changes for {owner}/{name}.")
    return 0


# --- run -------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc

    log_path = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    header = f"[{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M:%S}] --- tick start ---"

    def log(msg: str) -> None:
        print(msg)
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass  # don't crash the tick for a log write failure

    log(header)
    config = ws.load_config()
    cli_max = getattr(args, "max_issues", 0) or 0
    effective_max = cli_max if cli_max > 0 else config.max_issues_per_tick
    report = tick(ws, dry_run=args.dry_run, log=log, max_issues=effective_max)
    if report.error:
        print(f"\nError: {report.error}", file=sys.stderr)
        return 1
    if getattr(report, "skipped", False):
        return 0  # skip message already logged via the tick's `log` callback
    print(f"\nDone. {report.total_turns} agent turn(s) across {len(report.repos)} repo(s).")
    for cost_line in _format_cost_lines(report):
        log(cost_line)
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


# --- status ----------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc

    load_into_environ(ws.env_path)
    token = os.environ.get(GITHUB_TOKEN_KEY, "")
    if not token:
        print(f"{GITHUB_TOKEN_KEY} not set; add it to {ws.env_path}", file=sys.stderr)
        return 1

    config = ws.load_config()
    repos = [r for r in Registry(ws.registry_path).load() if r.enabled]
    if not repos:
        print("No enabled repositories registered. Add one with `cheaphelp repo add owner/name`.")
        return 0

    title_width = 60
    show_costs = getattr(args, "costs", False)
    try:
        with GitHubClient(token) as gh:
            bot_login = gh.authenticated_login()
            for repo in repos:
                print(repo.slug)
                try:
                    issues = gh.list_open_issues(repo.owner, repo.name)
                except GitHubError as exc:
                    print(f"  ! failed to list issues: {exc}", file=sys.stderr)
                    continue
                if not issues:
                    print("  (no open issues)")
                    continue
                for issue in issues:
                    try:
                        comments = gh.list_issue_comments(repo.owner, repo.name, issue.number)
                    except GitHubError as exc:
                        print(f"  ! failed to list comments for #{issue.number}: {exc}", file=sys.stderr)
                        comments = []
                    stage = classify(issue, comments, bot_login, config)
                    label = stage
                    title = issue.title[: title_width - 1] + "\u2026" if len(issue.title) > title_width else issue.title
                    if show_costs:
                        cost = IssueCostStore(ws.issue_dir(repo.owner, repo.name, issue.number)).load()
                        print(f"  #{issue.number:<6} {title:<{title_width}}  {label}  ${cost.cost_usd:.3f}")
                    else:
                        print(f"  #{issue.number:<6} {title:<{title_width}}  {label}")
    except GitHubError as exc:
        print(f"GitHub API error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc

    dry_run = bool(getattr(args, "dry_run", False))
    repos = Registry(ws.registry_path).load()
    total = 0

    # Clones for repos that are no longer registered (no token required).
    total += len(cleanup.prune_orphan_clones(ws, repos, dry_run=dry_run, log=print))

    # Build clones for closed issues of each registered repo (needs the open set).
    load_into_environ(ws.env_path)
    token = os.environ.get(GITHUB_TOKEN_KEY, "")
    if token and repos:
        try:
            with GitHubClient(token) as gh:
                for repo in repos:
                    try:
                        open_numbers = {i.number for i in gh.list_open_issues(repo.owner, repo.name)}
                    except GitHubError as exc:
                        print(f"  ! {repo.slug}: could not list issues: {exc}", file=sys.stderr)
                        continue
                    total += len(cleanup.prune_repo_work_clones(ws, repo, open_numbers, dry_run=dry_run, log=print))
        except GitHubError as exc:
            print(f"GitHub API error: {exc}", file=sys.stderr)
            return 1
    elif not token:
        print(f"({GITHUB_TOKEN_KEY} not set; swept orphans only — per-issue pruning needs a token.)")

    verb = "Would remove" if dry_run else "Removed"
    print(f"{verb} {total} clone(s).")
    return 0


# --- logs ------------------------------------------------------------------
def cmd_logs(args: argparse.Namespace) -> int:
    """Display tick activity from the run log for today.

    Resolves the workspace via ``_workspace(args)`` and gates on
    ``_require_workspace``.  Reads the daily log file, optionally filters by
    issue number (``#<n>``), and can stream new lines as they are appended.

    Parameters:
        args: Parsed command-line namespace.  Expected attributes:
            ``home`` (optional), ``issue`` (int or None), ``follow`` (bool).

    Returns:
        Exit code (0 on success).
    """
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc

    log_path = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    issue: int | None = getattr(args, "issue", None)
    follow: bool = bool(getattr(args, "follow", False))

    if not log_path.exists():
        print(f"(no log for today; expected {log_path.name})", file=sys.stderr)
        return 0

    if not follow:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"Failed to read log: {exc}", file=sys.stderr)
            return 1
        lines = text.splitlines()
        tail = lines[-_LOG_TAIL_LINES:] if len(lines) > _LOG_TAIL_LINES else lines
        for line in tail:
            if issue is None or f"#{issue}" in line:
                print(line)
        return 0

    # --follow path: tail -f semantics, only new appends.
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while True:
                try:
                    chunk = f.read()
                    if chunk:
                        for line in chunk.splitlines():
                            if issue is None or f"#{issue}" in line:
                                print(line)
                        sys.stdout.flush()
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    return 0
    except OSError as exc:
        print(f"Failed to follow log: {exc}", file=sys.stderr)
        return 1


def add_config_overrides(config: Config) -> None:  # pragma: no cover - reserved
    """Placeholder for future per-invocation config overrides."""


# --- config ------------------------------------------------------------------
_CONFIG_SCALAR_KEYS: dict[str, type] = {
    "version": int,
    "poll_interval": str,
    "opencode_bin": str,
    "agent_timeout": float,
    "max_issues_per_tick": int,
    "max_tasks_per_tick": int,
    "max_task_attempts": int,
    "prune_work_clones": bool,
}

_CONFIG_DICT_KEYS: dict[str, dict[str, type]] = {
    "models": {"responder": str, "planner": str, "worker": str, "reviewer": str},
    "variants": {"responder": str, "planner": str, "worker": str, "reviewer": str},
}

_OPENSCODE_AFFECTED: set[str] = {"models", "variants"}


def _validate_known_path(path: str) -> tuple[str, str | None, type]:
    """Validate a dotted config path and return ``(top_key, sub_key, py_type)``.

    Raises:
        ValueError: If the path is unknown.
    """
    parts = path.split(".", 1) if "." in path else (path, None)
    top = parts[0]
    # Check scalar keys first (no sub-key allowed).
    if top in _CONFIG_SCALAR_KEYS:
        if parts[1] is not None:
            raise ValueError(f"Unknown config key: {path}")
        return top, None, _CONFIG_SCALAR_KEYS[top]

    # Check dict keys.
    if top in _CONFIG_DICT_KEYS:
        if parts[1] is None:
            raise ValueError(f"Unknown config key: {path}")
        sub = parts[1]
        # Reject deeper nesting (dotted path within a dict sub-key).
        if "." in sub:
            raise ValueError(f"Unknown config key: {path}")
        sub_types = _CONFIG_DICT_KEYS[top]
        if sub not in sub_types:
            raise ValueError(f"Unknown config key: {path}")
        return top, sub, sub_types[sub]

    raise ValueError(f"Unknown config key: {top}")


def _coerce(raw: str, py_type: type) -> Any:
    """Parse *raw* into *py_type*, raising ``ValueError`` on failure."""
    if py_type is bool:
        lower = raw.lower()
        if lower in ("true", "1"):
            return True
        if lower in ("false", "0"):
            return False
        raise ValueError(f"Expected bool, got {raw!r}")
    if py_type is int:
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"Expected int, got {raw!r}") from None
    if py_type is float:
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"Expected float, got {raw!r}") from None
    if py_type is str:
        return raw
    raise ValueError(f"Expected {py_type.__name__}, got {raw!r}")


def _load_raw_overrides(ws: Workspace) -> dict:
    """Read the raw JSON from *ws.config_path*, or return ``{}``."""
    if not ws.config_path.exists():
        return {}
    return json.loads(ws.config_path.read_text(encoding="utf-8"))


def _format_value(value: Any, py_type: type) -> str:
    """Format *value* for display according to *py_type*."""
    if py_type is str:
        return repr(value)
    if py_type is float:
        # Always use str() so e.g. 600.0 prints as "600.0".
        return str(value)
    if py_type is bool:
        return str(value).lower()
    # int and others
    return str(value)


def cmd_config_show(args: argparse.Namespace) -> int:
    """Print the effective configuration."""
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc

    config = ws.load_config()
    raw = _load_raw_overrides(ws)
    config_dict = config.to_dict()

    print(f"# {ws.config_path}")

    # Scalars.
    for key, py_type in _CONFIG_SCALAR_KEYS.items():
        value = getattr(config, key)
        suffix = ""
        if key not in raw:
            suffix = "  (default)"
        print(f"  {key}: {_format_value(value, py_type)}  ({py_type.__name__}){suffix}")

    # Dict sections.
    for top, sub_types in _CONFIG_DICT_KEYS.items():
        print()
        print(f"  {top}:")
        raw_top = raw.get(top, {}) if isinstance(raw.get(top), dict) else {}
        for sub_key, py_type in sub_types.items():
            value = config_dict[top][sub_key]
            suffix = ""
            if sub_key not in raw_top:
                suffix = "  (default)"
            print(f"    {sub_key}: {_format_value(value, py_type)}{suffix}")

    # pr_reviewers (read-only, not configurable via set).
    print()
    print(f"  pr_reviewers: {config_dict.get('pr_reviewers', [])}")

    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    """Look up a single config value by dotted path."""
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc

    config = ws.load_config()

    try:
        top, sub, py_type = _validate_known_path(args.key)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    value = getattr(config, top) if sub is None else getattr(config, top)[sub]

    print(_format_value(value, py_type))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    """Set a config value by dotted path."""
    ws = _workspace(args)
    if (rc := _require_workspace(ws)) is not None:
        return rc

    try:
        top, sub, py_type = _validate_known_path(args.key)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        coerced = _coerce(args.value, py_type)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    config = ws.load_config()

    if sub is None:
        setattr(config, top, coerced)
    else:
        getattr(config, top)[sub] = coerced

    ws.save_config(config)

    if top in _OPENSCODE_AFFECTED:
        path = opencode.write_opencode_config(ws, config)
        print(f"Regenerated {path}")

    print(f"Set {args.key} = {_format_value(coerced, py_type)}")
    return 0
