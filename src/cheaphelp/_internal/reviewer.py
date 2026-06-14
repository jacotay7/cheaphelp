"""Reviewer turn logic.

When all of an issue's tasks are done, the reviewer inspects the combined diff
and either opens a pull request (for a human to merge) or sends the issue back to
the planner with notes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal import gitutil, opencode, pr_state
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.conventions import read_conventions
from cheaphelp._internal.github import GitHubClient
from cheaphelp._internal.opencode import UsageData
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.responder import cheaphelp_message
from cheaphelp._internal.tasks import TaskStore
from cheaphelp._internal.worker import branch_name

# Keep the diff we send to the model bounded; cheap context budgets.
_MAX_DIFF_CHARS = 40_000


def _collect_summaries(store: TaskStore) -> str:
    parts = []
    for task in store.load():
        summary_path = store.tasks_subdir / f"{task.id}.summary.md"
        summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else "(no summary)"
        parts.append(f"### {task.id}: {task.title} ({task.status})\n\n{summary}")
    return "\n\n".join(parts)


def build_prompt(issue_md: str, name_status: str, full_diff: str, summaries: str, *, conventions: str = "") -> str:
    """Render the reviewer's user message."""
    diff = full_diff
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n\n... (diff truncated) ...\n"
    lines = [
        "## Original specification (issues.md)",
        "",
        issue_md.strip() or "_(no spec)_",
        "",
        "## Task summaries",
        "",
        summaries or "_(none)_",
        "",
        "## Changed files",
        "",
        "```",
        name_status or "(no changes)",
        "```",
        "",
        "## Full diff (branch vs base)",
        "",
        "```diff",
        diff or "(empty)",
        "```",
    ]
    if conventions.strip():
        lines += [
            "",
            "## Repository conventions",
            "",
            conventions.rstrip(),
        ]
    lines += [
        "",
        "---",
        "",
        "Review the combined result and decide, following your output protocol (a single json block).",
    ]
    return "\n".join(lines)


@dataclass
class ReviewResult:
    """Outcome of a reviewer turn, for logging."""

    number: int
    decision: str
    pr_url: str | None = None
    error: str | None = None
    usage: UsageData | None = None


def _route_push_failure_to_human(
    gh: GitHubClient,
    config: Config,
    repo: RepoEntry,
    number: int,
    exc: Exception,
) -> None:
    """Label an issue ``needs-human`` after a non-retryable push failure.

    Mirrors the blast-radius escape hatch: post an attributed comment with the
    git error, add ``needs-human`` and drop ``planned``/``in-progress`` so the
    orchestrator stops re-running the build (and re-crashing) on every tick.
    """
    body = (
        "The implementation is complete, but pushing the branch to open a pull "
        "request was rejected, so no PR could be opened. This usually needs a "
        "human to fix the cause (for example, the GitHub token may be missing the "
        "`workflow` scope required to push changes under `.github/workflows/`).\n\n"
        f"```\n{str(exc).strip()}\n```"
    )
    gh.ensure_label(
        repo.owner,
        repo.name,
        config.labels["needs_human"],
        color="d93f0b",
        description="cheaphelp: stuck; needs a human",
    )
    gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_human"]])
    gh.remove_label(repo.owner, repo.name, number, config.labels["planned"])
    gh.remove_label(repo.owner, repo.name, number, config.labels["in_progress"])
    gh.create_comment(repo.owner, repo.name, number, cheaphelp_message(body, "reviewer", config))


def _save_pr_state(issue_dir: Path, pr: dict, reviewers: list[str], clone_dir: Path) -> None:
    """Write ``pr_state.json`` so the rework stage can find the PR on later ticks."""
    try:
        last_push_sha = gitutil.rev_parse(clone_dir)
    except gitutil.GitError:
        last_push_sha = ""
    pr_state.save_pr_state(
        issue_dir,
        {
            "pr_number": int(pr.get("number", 0)),
            "pr_url": str(pr.get("html_url", "")),
            "last_push_sha": last_push_sha,
            "reviewers": reviewers,
            "rework_attempts": 0,
        },
    )


def apply_review(
    gh: GitHubClient,
    workspace: Workspace,
    config: Config,
    repo: RepoEntry,
    number: int,
    decision: dict,
    clone_dir: Path,
    *,
    token: str | None,
    usage: UsageData | None = None,
) -> ReviewResult:
    """Act on a reviewer decision: open a PR or send back to the planner."""
    choice = str(decision.get("decision", "")).strip().lower()
    issue_dir = workspace.issue_dir(repo.owner, repo.name, number)
    branch = branch_name(number)

    if choice == "open_pr":
        if os.environ.get("CHEAPHELP_NO_PUSH"):
            # Inspection mode: don't touch the remote. Leave the issue as-is so a
            # later run (without the guard) actually opens the PR.
            return ReviewResult(number=number, decision="open_pr (skipped: NO_PUSH)", usage=usage)
        # Make sure the branch is on the remote before opening the PR. A push can
        # be rejected for reasons a retry will never fix (e.g. the token lacks the
        # `workflow` scope and the branch touches `.github/workflows/`). Route the
        # issue to a human instead of crashing the build every tick.
        try:
            gitutil.push_branch(clone_dir, repo, branch=branch, token=token)
        except Exception as exc:  # noqa: BLE001
            _route_push_failure_to_human(gh, config, repo, number, exc)
            return ReviewResult(number=number, decision="push_failed", error=str(exc), usage=usage)
        title = str(decision.get("pr_title") or f"cheaphelp: resolve #{number}").strip()
        body = str(decision.get("pr_body") or "").strip()
        reviewers = config.pr_reviewers or [repo.owner]
        mentions = " ".join(f"@{r}" for r in reviewers)
        body = cheaphelp_message(
            f"{body}\n\nCloses #{number}\n\n"
            f"Requested reviewer(s): {mentions}\n\n"
            "_Opened by cheaphelp; awaiting human review._",
            "reviewer",
            config,
        )
        try:
            pr = gh.create_pull_request(
                repo.owner,
                repo.name,
                title=title,
                head=branch,
                base=repo.default_branch or "main",
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            return ReviewResult(number=number, decision=choice, error=str(exc), usage=usage)
        # Best-effort formal review request. GitHub rejects requesting the PR
        # author (common when the bot is the repo owner); the @mention above
        # still notifies them in that case.
        gh.request_reviewers(repo.owner, repo.name, int(pr.get("number", 0)), reviewers)
        # Persist the PR link so the rework stage can find it on later ticks.
        _save_pr_state(issue_dir, pr, reviewers, clone_dir)
        gh.ensure_label(
            repo.owner,
            repo.name,
            config.labels["in_review"],
            color="5319e7",
            description="cheaphelp: PR open, awaiting human review",
        )
        gh.add_labels(repo.owner, repo.name, number, [config.labels["in_review"]])
        gh.remove_label(repo.owner, repo.name, number, config.labels["planned"])
        gh.remove_label(repo.owner, repo.name, number, config.labels["in_progress"])
        gh.create_comment(
            repo.owner,
            repo.name,
            number,
            cheaphelp_message(f"Opened a pull request for review: {pr.get('html_url', '')}", "reviewer", config),
        )
        return ReviewResult(number=number, decision=choice, pr_url=pr.get("html_url"), usage=usage)

    # replan
    notes = str(decision.get("replan_notes") or "").strip()
    (issue_dir / "replan.md").write_text(notes + "\n", encoding="utf-8")
    gh.ensure_label(
        repo.owner,
        repo.name,
        config.labels["needs_replan"],
        color="fbca04",
        description="cheaphelp: reviewer sent back to planner",
    )
    gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_replan"]])
    gh.remove_label(repo.owner, repo.name, number, config.labels["planned"])
    gh.remove_label(repo.owner, repo.name, number, config.labels["in_progress"])
    gh.create_comment(
        repo.owner,
        repo.name,
        number,
        cheaphelp_message(f"Sending this back to planning:\n\n{notes}", "reviewer", config),
    )
    return ReviewResult(number=number, decision="replan", usage=usage)


def review_issue(
    gh: GitHubClient,
    workspace: Workspace,
    config: Config,
    repo: RepoEntry,
    number: int,
    clone_dir: Path,
    *,
    token: str | None,
) -> ReviewResult:
    """Gather the diff + summaries, run the reviewer agent, and apply its decision."""
    issue_dir = workspace.issue_dir(repo.owner, repo.name, number)
    store = TaskStore(issue_dir)
    issue_md_path = issue_dir / "issues.md"
    issue_md = issue_md_path.read_text(encoding="utf-8") if issue_md_path.exists() else ""
    name_status, full_diff = gitutil.diff_against_base(clone_dir, repo)
    conventions = read_conventions(clone_dir)
    prompt = build_prompt(issue_md, name_status, full_diff, _collect_summaries(store), conventions=conventions)

    result = opencode.run_agent(workspace, config, "reviewer", prompt, cwd=clone_dir, timeout=config.agent_timeout)
    usage = result.usage
    if result.decision is None:
        return ReviewResult(number=number, decision="none", error="unparseable", usage=usage)
    return apply_review(gh, workspace, config, repo, number, result.decision, clone_dir, token=token, usage=usage)
