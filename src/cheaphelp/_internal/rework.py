"""Rework turn logic.

When a PR has unaddressed human review feedback, the rework agent edits files
on the same branch, commits, pushes, and requests re-review. The stage runs
every tick until the PR is merged or the rework agent gets stuck.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

from cheaphelp._internal import gitutil, opencode, pr_state
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import GitHubClient
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.responder import cheaphelp_message
from cheaphelp._internal.worker import branch_name

# Keep the diff we send to the model bounded; cheap context budgets.
MAX_DIFF_CHARS = 40_000

# Known bot account logins (in addition to the token's own login) whose
# feedback should never trigger a rework response.
_BOT_USERS = {"github-actions[bot]"}


def _is_human(user: str, bot_login: str) -> bool:
    """Return ``True`` when *user* is a non-empty, non-bot login."""
    if not user:
        return False
    if user == bot_login:
        return False
    return user not in _BOT_USERS


def _is_unaddressed(item, last_push_committed_at: str) -> bool:  # noqa: ANN001
    """Whether *item* (a review dict or a PRReviewComment) is new since the last push.

    A missing or malformed timestamp is treated as unaddressed (new) so that
    borderline cases are acted on rather than silently dropped. ``APPROVED``
    reviews are always excluded — we only act on ``CHANGES_REQUESTED`` and
    ``COMMENTED``.
    """
    if isinstance(item, dict):
        if item.get("state") == "APPROVED":
            return False
        ts = item.get("submitted_at", "")
    else:
        ts = item.created_at

    if not ts or not last_push_committed_at:
        return True

    try:
        item_time = datetime.fromisoformat(ts)
        push_time = datetime.fromisoformat(last_push_committed_at)
    except (ValueError, TypeError):
        return True

    return item_time > push_time


def build_prompt(issue_md: str, name_status: str, full_diff: str, feedback_blocks: list[dict]) -> str:
    """Render the rework agent's user message.

    Sections:
    1. Original spec (``issues.md``).
    2. Current branch's ``git diff --name-status`` and a bounded full diff.
    3. A numbered list of feedback items, each with the reviewer's login, file
       (if inline), body, and type marker.
    4. Closing instruction to address every item.
    """
    diff = full_diff
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n... (diff truncated) ...\n"

    lines = [
        "## Original specification (issues.md)",
        "",
        issue_md.strip() or "_(no spec)_",
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
        "## Review feedback to address",
        "",
    ]

    if not feedback_blocks:
        lines.append("_(no new feedback)_")
    else:
        for i, fb in enumerate(feedback_blocks, 1):
            fb_type = fb.get("type", "review")
            login = fb.get("login", "unknown")
            body = fb.get("body", "")
            if fb_type == "comment":
                path = fb.get("path", "")
                lines.append(f"{i}. **@{login}** (inline comment on `{path}`): {body}")
            else:
                state = fb.get("state", "COMMENTED")
                lines.append(f"{i}. **@{login}** (formal review: `{state}`): {body}")

    lines.extend(
        [
            "",
            "---",
            "",
            "Address every feedback item above. Edit files in the working directory, "
            "verify your change, and reply with a single ```json block.",
        ],
    )

    return "\n".join(lines)


@dataclass
class ReworkResult:
    """Outcome of a rework turn, for logging."""

    number: int
    status: str
    committed: bool = False
    pushed: bool = False
    error: str | None = None
    summary: str = ""


def run_rework(
    gh: GitHubClient,
    workspace: Workspace,
    config: Config,
    repo: RepoEntry,
    number: int,
    *,
    token: str | None,
) -> ReworkResult:
    """Run one rework turn end-to-end.

    1. Load ``pr_state.json`` — return ``no_feedback`` if missing.
    2. Check whether the PR is still open.
    3. Fetch reviews and inline comments; build a candidate feedback list.
    4. If no new human feedback, return ``no_feedback`` (no-op).
    5. Ensure the work clone exists on the issue branch.
    6. Run the rework agent.
    7. On success: commit, push, post a PR comment, re-request reviewers.
    8. On timeout / unparseable: escalate after ``max_task_attempts``.
    """
    try:
        return _run_rework(gh, workspace, config, repo, number, token=token)
    except Exception as exc:  # noqa: BLE001
        return ReworkResult(number=number, status="error", error=str(exc))


def _run_rework(
    gh: GitHubClient,
    workspace: Workspace,
    config: Config,
    repo: RepoEntry,
    number: int,
    *,
    token: str | None,
) -> ReworkResult:
    """Inner implementation of ``run_rework`` (wrapped for top-level resilience)."""
    issue_dir = workspace.issue_dir(repo.owner, repo.name, number)
    pr_state_data = pr_state.load_pr_state(issue_dir)

    if pr_state_data is None:
        return ReworkResult(number=number, status="no_feedback", error="no pr_state")

    bot_login = gh.authenticated_login()
    pr_number = int(pr_state_data.get("pr_number", 0))
    last_push_sha: str = pr_state_data.get("last_push_sha", "") or ""
    rework_attempts = int(pr_state_data.get("rework_attempts", 0))

    # ── last-push commit timestamp ─────────────────────────────────────────
    last_push_committed_at = ""
    if last_push_sha:
        with contextlib.suppress(gitutil.GitError):
            last_push_committed_at = gitutil._run(["log", "-1", "--format=%cI", last_push_sha], cwd=workspace.home)

    # ── is the PR still open? ──────────────────────────────────────────────
    try:
        pr = gh.get_pull_request(repo.owner, repo.name, pr_number)
    except Exception as exc:  # noqa: BLE001
        return ReworkResult(number=number, status="error", error=str(exc))

    if pr.get("state") == "closed":
        return ReworkResult(number=number, status="no_feedback")

    # ── fetch reviews and inline comments ──────────────────────────────────
    try:
        reviews = gh.list_pr_reviews(repo.owner, repo.name, pr_number)
    except Exception:  # noqa: BLE001
        reviews = []

    try:
        comments = gh.list_pr_review_comments(repo.owner, repo.name, pr_number)
    except Exception:  # noqa: BLE001
        comments = []

    # ── build candidate feedback list ──────────────────────────────────────
    feedback_blocks: list[dict] = []

    for review in reviews:
        state = review.get("state", "")
        if state not in {"CHANGES_REQUESTED", "COMMENTED"}:
            continue
        user = (review.get("user") or {}).get("login", "")
        if not _is_human(user, bot_login):
            continue
        if not _is_unaddressed(review, last_push_committed_at):
            continue
        feedback_blocks.append(
            {
                "type": "review",
                "login": user,
                "state": state,
                "body": review.get("body", ""),
            },
        )

    for comment in comments:
        user = comment.user
        if not _is_human(user, bot_login):
            continue
        if not _is_unaddressed(comment, last_push_committed_at):
            continue
        feedback_blocks.append(
            {
                "type": "comment",
                "login": user,
                "path": comment.path,
                "body": comment.body,
            },
        )

    if not feedback_blocks:
        return ReworkResult(number=number, status="no_feedback")

    # ── ensure work clone ──────────────────────────────────────────────────
    branch = branch_name(number)
    work_dir = workspace.work_clone_path(repo.owner, repo.name, number)
    try:
        gitutil.ensure_work_clone(work_dir, repo, token=token, branch=branch)
    except Exception as exc:  # noqa: BLE001
        return ReworkResult(number=number, status="error", error=str(exc))

    # ── build prompt and run agent ─────────────────────────────────────────
    issue_md_path = issue_dir / "issues.md"
    issue_md = issue_md_path.read_text(encoding="utf-8") if issue_md_path.exists() else ""
    name_status, full_diff = gitutil.diff_against_base(work_dir, repo)
    prompt = build_prompt(issue_md, name_status, full_diff, feedback_blocks)

    try:
        result = opencode.run_agent(
            workspace,
            config,
            "rework",
            prompt,
            cwd=work_dir,
            timeout=config.agent_timeout,
            issue_dir=issue_dir,
        )
    except subprocess.TimeoutExpired:
        new_attempts = rework_attempts + 1
        pr_state.save_pr_state(issue_dir, {**pr_state_data, "rework_attempts": new_attempts, "last_error": "timeout"})
        if new_attempts >= config.max_task_attempts:
            gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_human"]])
            gh.remove_label(repo.owner, repo.name, number, config.labels["in_review"])
            gh.create_comment(
                repo.owner,
                repo.name,
                number,
                cheaphelp_message(
                    f"Rework agent timed out after {new_attempts} attempt(s). Escalating to human.",
                    "rework",
                    config,
                ),
            )
            return ReworkResult(number=number, status="escalated", error="timeout")
        return ReworkResult(number=number, status="blocked", error="timeout")

    if result.decision is None:
        new_attempts = rework_attempts + 1
        pr_state.save_pr_state(
            issue_dir,
            {**pr_state_data, "rework_attempts": new_attempts, "last_error": "unparseable"},
        )
        if new_attempts >= config.max_task_attempts:
            gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_human"]])
            gh.remove_label(repo.owner, repo.name, number, config.labels["in_review"])
            gh.create_comment(
                repo.owner,
                repo.name,
                number,
                cheaphelp_message(
                    f"Rework agent produced no parseable result after {new_attempts} attempt(s). Escalating to human. (see `last_unparsed_rework.log`)",
                    "rework",
                    config,
                ),
            )
            return ReworkResult(number=number, status="escalated", error="unparseable")
        return ReworkResult(number=number, status="blocked", error="unparseable")

    decision = result.decision or {}
    status = str(decision.get("status", "")).strip().lower()
    summary = str(decision.get("summary", "")).strip()
    notes = str(decision.get("notes", "")).strip()

    if status == "done":
        committed = gitutil.commit_all(work_dir, message=f"cheaphelp rework: #{number}")

        # CHEAPHELP_NO_PUSH / GH_NO_PUSH: let the operator inspect changes locally.
        _no_push = os.environ.get("CHEAPHELP_NO_PUSH") or os.environ.get("GH_NO_PUSH")
        pushed = False
        if committed and not _no_push:
            try:
                gitutil.push_branch(work_dir, repo, branch=branch, token=token)
                pushed = True
            except gitutil.GitError:
                pushed = False

        # Update last_push_sha and reset rework_attempts.
        try:
            new_sha = gitutil.rev_parse(work_dir)
        except gitutil.GitError:
            new_sha = ""

        pr_state.save_pr_state(
            issue_dir,
            {
                **pr_state_data,
                "last_push_sha": new_sha or last_push_sha,
                "rework_attempts": 0,
                "last_error": "",
            },
        )

        # Post a PR comment summarising the changes.
        full_summary = summary + (f"\n\n**Notes:** {notes}" if notes else "")
        gh.create_comment(
            repo.owner,
            repo.name,
            pr_number,
            cheaphelp_message(full_summary or "(no summary)", "rework", config),
        )

        # Re-request review from the original reviewers.
        reviewers = pr_state_data.get("reviewers", [])
        if reviewers:
            gh.request_reviewers(repo.owner, repo.name, pr_number, reviewers)

        return ReworkResult(number=number, status="done", committed=committed, pushed=pushed, summary=summary)

    # Any non-"done" status: reset attempts so the next tick gets a clean
    # attempt counter for a different blocker.
    pr_state.save_pr_state(issue_dir, {**pr_state_data, "rework_attempts": 0, "last_error": ""})
    return ReworkResult(number=number, status="blocked", summary=summary)
