# Why does this file exist, and why not put this in `__main__`?
#
# You might be tempted to import things from `__main__` later,
# but that will cause problems: the code will get executed twice:
#
# - When you run `python -m cheaphelp` python will execute
#   `__main__.py` as a script. That means there won't be any
#   `cheaphelp.__main__` in `sys.modules`.
# - When you import `__main__` it will get executed again (as a module) because
#   there's no `cheaphelp.__main__` in `sys.modules`.

from __future__ import annotations

import argparse
import sys
from typing import Any

from cheaphelp._internal import commands, debug


class _DebugInfo(argparse.Action):
    def __init__(self, nargs: int | str | None = 0, **kwargs: Any) -> None:
        super().__init__(nargs=nargs, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        debug._print_debug_info()
        sys.exit(0)


def get_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser.

    Returns:
        An argparse parser.
    """
    parser = argparse.ArgumentParser(
        prog="cheaphelp",
        description="An AI software-engineer for your GitHub repositories, powered by "
        "cheap OpenRouter models via the opencode harness.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {debug._get_version()}")
    parser.add_argument("--debug-info", action=_DebugInfo, help="Print debug information.")
    parser.add_argument(
        "--home",
        metavar="DIR",
        help="Workspace directory (default: $CHEAPHELP_HOME or ~/.cheaphelp).",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # init
    p_init = subparsers.add_parser("init", help="Create or update the workspace.")
    p_init.add_argument("--github-token", help="GitHub personal access token.")
    p_init.add_argument("--openrouter-key", help="OpenRouter API key.")
    p_init.add_argument("--no-prompt", action="store_true", help="Never prompt for secrets.")
    p_init.set_defaults(func=commands.cmd_init)

    # repo
    p_repo = subparsers.add_parser("repo", help="Manage registered repositories.")
    repo_sub = p_repo.add_subparsers(dest="repo_command", metavar="<action>")
    p_add = repo_sub.add_parser("add", help="Register a repository (owner/name).")
    p_add.add_argument("slug", help="Repository as owner/name or a GitHub URL.")
    p_add.add_argument(
        "--checks",
        default="",
        help="Quality-gate shell command run in the clone before a PR is opened "
        '(e.g. "ruff check . && pytest"). A failing gate sends the issue back to planning.',
    )
    p_add.add_argument(
        "--autofix",
        default="",
        help="Shell command run in the clone before the gate to auto-fix trivial "
        'issues (e.g. "ruff check --fix . ; ruff format ."). Changes are committed.',
    )
    p_add.add_argument(
        "--max-diff-files",
        type=int,
        default=None,
        metavar="N",
        help="Maximum files allowed in a single PR for this repo (0 = unlimited, default 30).",
    )
    p_add.add_argument(
        "--max-diff-lines",
        type=int,
        default=None,
        metavar="N",
        help="Maximum lines (added+removed) allowed in a single PR (0 = unlimited, default 1000).",
    )
    p_add.set_defaults(func=commands.cmd_repo_add)
    p_list = repo_sub.add_parser("list", help="List registered repositories.")
    p_list.add_argument(
        "--json",
        action="store_true",
        help="Output the list of registered repositories as a JSON array.",
    )
    p_list.set_defaults(func=commands.cmd_repo_list)
    p_rm = repo_sub.add_parser("remove", help="Unregister a repository.")
    p_rm.add_argument("slug")
    p_rm.set_defaults(func=commands.cmd_repo_remove)
    p_set = repo_sub.add_parser(
        "set",
        help="Update checks/autofix on an already-registered repository.",
    )
    p_set.add_argument("slug", help="Repository as owner/name or a GitHub URL.")
    p_set.add_argument(
        "--checks",
        default=None,
        help="Replace the quality-gate shell command. Pass an empty string to disable the gate.",
    )
    p_set.add_argument(
        "--autofix",
        default=None,
        help="Replace the auto-fix shell command. Pass an empty string to disable auto-fix.",
    )
    p_set.add_argument(
        "--max-diff-files",
        type=int,
        default=None,
        metavar="N",
        help="Maximum files allowed in a single PR for this repo (0 = unlimited, default 30).",
    )
    p_set.add_argument(
        "--max-diff-lines",
        type=int,
        default=None,
        metavar="N",
        help="Maximum lines (added+removed) allowed in a single PR (0 = unlimited, default 1000).",
    )
    p_set.set_defaults(func=commands.cmd_repo_set)
    p_update = repo_sub.add_parser(
        "update",
        help="Update checks/autofix/max-diff-* on an already-registered repository (alias of `set`).",
    )
    p_update.add_argument("slug", help="Repository as owner/name or a GitHub URL.")
    p_update.add_argument(
        "--checks",
        default=None,
        help="Replace the quality-gate shell command. Pass an empty string to disable the gate.",
    )
    p_update.add_argument(
        "--autofix",
        default=None,
        help="Replace the auto-fix shell command. Pass an empty string to disable auto-fix.",
    )
    p_update.add_argument(
        "--max-diff-files",
        type=int,
        default=None,
        metavar="N",
        help="Maximum files allowed in a single PR for this repo (0 = unlimited, default 30).",
    )
    p_update.add_argument(
        "--max-diff-lines",
        type=int,
        default=None,
        metavar="N",
        help="Maximum lines (added+removed) allowed in a single PR (0 = unlimited, default 1000).",
    )
    p_update.set_defaults(func=commands.cmd_repo_set)
    p_en = repo_sub.add_parser("enable", help="Enable processing for a repository.")
    p_en.add_argument("slug")
    p_en.set_defaults(func=lambda a: commands.cmd_repo_toggle(a, enabled=True))
    p_dis = repo_sub.add_parser("disable", help="Disable processing for a repository.")
    p_dis.add_argument("slug")
    p_dis.set_defaults(func=lambda a: commands.cmd_repo_toggle(a, enabled=False))

    # run
    p_run = subparsers.add_parser("run", help="Run one orchestrator tick.")
    p_run.add_argument("--once", action="store_true", help="Run a single tick (default behaviour).")
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without acting.",
    )
    p_run.add_argument(
        "--max-issues",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Cap the number of issues processed per repo in this tick "
            "(0 = unlimited, default 0). Overrides max_issues_per_tick from "
            "config.json when > 0."
        ),
    )
    p_run.set_defaults(func=commands.cmd_run)

    # systemd
    p_sys = subparsers.add_parser("systemd", help="Manage the systemd user timer.")
    sys_sub = p_sys.add_subparsers(dest="systemd_command", metavar="<action>")
    p_sys_install = sys_sub.add_parser("install", help="Install and start the timer.")
    p_sys_install.add_argument(
        "--interval",
        default="10m",
        help="Tick interval, e.g. 30s, 10m, 2h (default: 10m).",
    )
    p_sys_install.set_defaults(func=commands.cmd_systemd_install)
    sys_sub.add_parser("uninstall", help="Stop and remove the timer.").set_defaults(
        func=commands.cmd_systemd_uninstall,
    )
    sys_sub.add_parser("status", help="Show timer status.").set_defaults(
        func=commands.cmd_systemd_status,
    )

    # agents
    p_agents = subparsers.add_parser("agents", help="Manage agent prompts and opencode config.")
    agents_sub = p_agents.add_subparsers(dest="agents_command", metavar="<action>")
    p_sync = agents_sub.add_parser("sync", help="Regenerate opencode.json from prompts + config.")
    p_sync.add_argument("--force", action="store_true", help="Overwrite workspace prompts.")
    p_sync.set_defaults(func=commands.cmd_agents_sync)

    # doctor
    subparsers.add_parser("doctor", help="Check workspace, tokens and opencode.").set_defaults(
        func=commands.cmd_doctor,
    )

    # status
    subparsers.add_parser(
        "status",
        help="List open issues for each enabled repo and their pipeline stage.",
    ).set_defaults(func=commands.cmd_status)

    # clean
    p_clean = subparsers.add_parser(
        "clean",
        help="Remove build clones for closed issues and unregistered repos (keeps state).",
    )
    p_clean.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without deleting anything.",
    )
    p_clean.set_defaults(func=commands.cmd_clean)

    return parser


def main(args: list[str] | None = None) -> int:
    """Run the main program.

    This function is executed when you type `cheaphelp` or `python -m cheaphelp`.

    Parameters:
        args: Arguments passed from the command line.

    Returns:
        An exit code.
    """
    parser = get_parser()
    opts = parser.parse_args(args=args)

    func = getattr(opts, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return func(opts)
