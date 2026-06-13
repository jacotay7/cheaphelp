"""The orchestrator: one tick of the cheaphelp state machine.

A tick is what the systemd timer fires. For each enabled repository it polls
open issues and dispatches the responder to any issue waiting on a turn. Later
milestones will extend the same loop to dispatch the planner (on `ready`
issues), workers (on pending tasks) and the reviewer (on completed work).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from cheaphelp._internal import opencode, responder
from cheaphelp._internal.config import Workspace
from cheaphelp._internal.env import GITHUB_TOKEN_KEY, OPENROUTER_API_KEY, load_into_environ
from cheaphelp._internal.github import GitHubClient
from cheaphelp._internal.gitutil import ensure_clone
from cheaphelp._internal.registry import Registry, RepoEntry

Logger = Callable[[str], None]


def _is_mock() -> bool:
    return bool(os.environ.get("CHEAPHELP_AGENT_MOCK"))


@dataclass
class RepoReport:
    """Per-repository summary of a tick."""

    slug: str
    issues_considered: int = 0
    turns_taken: int = 0
    actions: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class TickReport:
    """Summary of a full orchestrator tick."""

    bot_login: str = ""
    dry_run: bool = False
    repos: list[RepoReport] = field(default_factory=list)
    error: str | None = None

    @property
    def total_turns(self) -> int:
        return sum(r.turns_taken for r in self.repos)


def _process_repo(
    gh: GitHubClient,
    workspace: Workspace,
    config,  # noqa: ANN001 - Config, avoid import cycle noise
    repo: RepoEntry,
    bot_login: str,
    token: str,
    *,
    dry_run: bool,
    log: Logger,
) -> RepoReport:
    report = RepoReport(slug=repo.slug)
    try:
        issues = gh.list_open_issues(repo.owner, repo.name)
    except Exception as exc:  # noqa: BLE001 - surface per-repo failures, keep going
        report.error = str(exc)
        log(f"  ! {repo.slug}: failed to list issues: {exc}")
        return report

    pending = []
    for issue in issues:
        comments = gh.list_issue_comments(repo.owner, repo.name, issue.number)
        if responder.needs_turn(issue, comments, bot_login, config):
            pending.append((issue, comments))
    report.issues_considered = len(pending)

    if not pending:
        log(f"  - {repo.slug}: nothing to do")
        return report

    if dry_run:
        for issue, _ in pending:
            log(f"  · {repo.slug}#{issue.number}: would run responder")
            report.actions.append(f"#{issue.number}: dry-run")
        return report

    # Clone the repo once so the responder can read the codebase.
    cwd = workspace.home
    if not _is_mock():
        try:
            cwd = ensure_clone(workspace.clone_path(repo.owner, repo.name), repo, token=token)
        except Exception as exc:  # noqa: BLE001
            report.error = f"clone failed: {exc}"
            log(f"  ! {repo.slug}: clone failed: {exc}")
            return report

    for issue, comments in pending:
        prompt = responder.build_prompt(issue, comments, bot_login)
        try:
            result = opencode.run_agent(workspace, config, "responder", prompt, cwd=cwd)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {repo.slug}#{issue.number}: agent error: {exc}")
            report.actions.append(f"#{issue.number}: agent error")
            continue

        if result.decision is None:
            log(f"  ! {repo.slug}#{issue.number}: no decision parsed (rc={result.returncode})")
            report.actions.append(f"#{issue.number}: unparseable")
            continue

        applied = responder.apply_decision(
            gh,
            workspace,
            config,
            repo.owner,
            repo.name,
            issue,
            result.decision,
        )
        report.turns_taken += 1
        summary = f"#{issue.number}: {applied.action}"
        if applied.error:
            summary += f" (error: {applied.error})"
        report.actions.append(summary)
        log(f"  > {repo.slug}{summary}")

    return report


def tick(workspace: Workspace, *, dry_run: bool = False, log: Logger | None = None) -> TickReport:
    """Run one orchestrator tick across all enabled repositories."""
    log = log or (lambda _msg: None)
    report = TickReport(dry_run=dry_run)

    if not workspace.exists():
        report.error = "workspace not initialised; run `cheaphelp init` first"
        return report

    load_into_environ(workspace.env_path)
    config = workspace.load_config()
    token = os.environ.get(GITHUB_TOKEN_KEY, "")

    if not token:
        report.error = f"{GITHUB_TOKEN_KEY} not set; add it to {workspace.env_path}"
        return report
    if not _is_mock() and not os.environ.get(OPENROUTER_API_KEY):
        log(f"  (warning: {OPENROUTER_API_KEY} not set; agent calls will fail)")

    repos = [r for r in Registry(workspace.registry_path).load() if r.enabled]
    if not repos:
        report.error = "no enabled repositories registered; use `cheaphelp repo add owner/name`"
        return report

    try:
        with GitHubClient(token) as gh:
            report.bot_login = gh.authenticated_login()
            log(f"acting as @{report.bot_login} ({'dry-run' if dry_run else 'live'})")
            for repo in repos:
                report.repos.append(
                    _process_repo(
                        gh,
                        workspace,
                        config,
                        repo,
                        report.bot_login,
                        token,
                        dry_run=dry_run,
                        log=log,
                    ),
                )
    except Exception as exc:  # noqa: BLE001 - top-level guard for the tick
        report.error = str(exc)

    return report
