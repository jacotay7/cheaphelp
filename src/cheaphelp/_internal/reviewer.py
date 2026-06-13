"""Reviewer turn logic.

When all of an issue's tasks are done, the reviewer inspects the combined diff
and either opens a pull request (for a human to merge) or sends the issue back to
the planner with notes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal import gitutil, opencode
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import GitHubClient
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.responder import BOT_MARKER
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


def build_prompt(issue_md: str, name_status: str, full_diff: str, summaries: str) -> str:
    """Render the reviewer's user message."""
    diff = full_diff
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n\n... (diff truncated) ...\n"
    return "\n".join(
        [
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
            "",
            "---",
            "",
            "Review the combined result and decide, following your output protocol "
            "(a single json block).",
        ],
    )


@dataclass
class ReviewResult:
    """Outcome of a reviewer turn, for logging."""

    number: int
    decision: str
    pr_url: str | None = None
    error: str | None = None


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
) -> ReviewResult:
    """Act on a reviewer decision: open a PR or send back to the planner."""
    choice = str(decision.get("decision", "")).strip().lower()
    issue_dir = workspace.issue_dir(repo.owner, repo.name, number)
    branch = branch_name(number)

    if choice == "open_pr":
        if os.environ.get("CHEAPHELP_NO_PUSH"):
            # Inspection mode: don't touch the remote. Leave the issue as-is so a
            # later run (without the guard) actually opens the PR.
            return ReviewResult(number=number, decision="open_pr (skipped: NO_PUSH)")
        # Make sure the branch is on the remote before opening the PR.
        gitutil.push_branch(clone_dir, repo, branch=branch, token=token)
        title = str(decision.get("pr_title") or f"cheaphelp: resolve #{number}").strip()
        body = str(decision.get("pr_body") or "").strip()
        reviewers = config.pr_reviewers or [repo.owner]
        mentions = " ".join(f"@{r}" for r in reviewers)
        body = (
            f"{body}\n\nCloses #{number}\n\n"
            f"Requested reviewer(s): {mentions}\n\n"
            "_Opened by cheaphelp; awaiting human review._"
        )
        try:
            pr = gh.create_pull_request(
                repo.owner, repo.name,
                title=title, head=branch, base=repo.default_branch or "main", body=body,
            )
        except Exception as exc:  # noqa: BLE001
            return ReviewResult(number=number, decision=choice, error=str(exc))
        # Best-effort formal review request. GitHub rejects requesting the PR
        # author (common when the bot is the repo owner); the @mention above
        # still notifies them in that case.
        gh.request_reviewers(repo.owner, repo.name, int(pr.get("number", 0)), reviewers)
        gh.ensure_label(repo.owner, repo.name, config.labels["in_review"], color="5319e7",
                        description="cheaphelp: PR open, awaiting human review")
        gh.add_labels(repo.owner, repo.name, number, [config.labels["in_review"]])
        gh.remove_label(repo.owner, repo.name, number, config.labels["planned"])
        gh.create_comment(repo.owner, repo.name, number,
                          f"{BOT_MARKER}\n\nOpened a pull request for review: {pr.get('html_url', '')}")
        return ReviewResult(number=number, decision=choice, pr_url=pr.get("html_url"))

    # replan
    notes = str(decision.get("replan_notes") or "").strip()
    (issue_dir / "replan.md").write_text(notes + "\n", encoding="utf-8")
    gh.ensure_label(repo.owner, repo.name, config.labels["needs_replan"], color="fbca04",
                    description="cheaphelp: reviewer sent back to planner")
    gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_replan"]])
    gh.remove_label(repo.owner, repo.name, number, config.labels["planned"])
    gh.create_comment(repo.owner, repo.name, number,
                      f"{BOT_MARKER}\n\nSending this back to planning:\n\n{notes}")
    return ReviewResult(number=number, decision="replan")


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
    prompt = build_prompt(issue_md, name_status, full_diff, _collect_summaries(store))

    result = opencode.run_agent(workspace, config, "reviewer", prompt, cwd=clone_dir)
    if result.decision is None:
        return ReviewResult(number=number, decision="none", error="unparseable")
    return apply_review(gh, workspace, config, repo, number, result.decision, clone_dir, token=token)
