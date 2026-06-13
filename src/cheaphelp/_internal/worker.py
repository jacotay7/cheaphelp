"""Worker turn logic.

A worker executes a single task in the issue's persistent work clone, on the
issue branch. The agent edits files; this module handles git (commit), records a
summary, and advances task state. One task is run per call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal import gitutil, opencode
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.tasks import BLOCKED, DONE, Task, TaskStore


def branch_name(number: int) -> str:
    """The working branch for an issue."""
    return f"cheaphelp/issue-{number}"


def build_prompt(task: Task, issue_md: str) -> str:
    """Render the worker's user message for a single task."""
    return "\n".join(
        [
            "You are implementing ONE task that is part of a larger issue.",
            "",
            "## Issue context (for background only — do not implement the whole issue)",
            "",
            issue_md.strip() or "_(no spec)_",
            "",
            "## Your task",
            "",
            task.to_markdown(),
            "",
            "---",
            "",
            "Implement this task in the working directory, verify it, then report "
            "following your output protocol (a single json block).",
        ],
    )


@dataclass
class WorkResult:
    """Outcome of a worker turn, for logging."""

    task_id: str
    status: str
    committed: bool = False
    error: str | None = None


def run_task(
    workspace: Workspace,
    config: Config,
    repo: RepoEntry,
    number: int,
    task: Task,
    clone_dir: Path,
    *,
    token: str | None,
) -> WorkResult:
    """Run one task end-to-end: agent edits, parse result, commit, record state."""
    store = TaskStore(workspace.issue_dir(repo.owner, repo.name, number))
    issue_md_path = workspace.issue_dir(repo.owner, repo.name, number) / "issues.md"
    issue_md = issue_md_path.read_text(encoding="utf-8") if issue_md_path.exists() else ""

    store.set_status(task.id, "in_progress")
    prompt = build_prompt(task, issue_md)
    result = opencode.run_agent(workspace, config, "worker", prompt, cwd=clone_dir)

    decision = result.decision or {}
    status = str(decision.get("status", "")).strip().lower()
    summary = str(decision.get("summary", "")).strip()
    notes = str(decision.get("notes", "")).strip()

    if result.decision is None:
        # No parseable decision: leave whatever changes exist but mark blocked.
        store.set_status(task.id, BLOCKED, summary="Worker produced no parseable result.")
        return WorkResult(task_id=task.id, status=BLOCKED, error="unparseable")

    full_summary = summary + (f"\n\n**Notes:** {notes}" if notes else "")
    store.write_summary(task.id, full_summary or "(no summary)")

    committed = False
    if status == "done":
        committed = gitutil.commit_all(clone_dir, message=f"cheaphelp {task.id}: {task.title}")
        store.set_status(task.id, DONE, summary=full_summary)
        # CHEAPHELP_NO_PUSH lets you inspect local commits before anything hits
        # the remote (useful for first runs and offline testing).
        if committed and not os.environ.get("CHEAPHELP_NO_PUSH"):
            gitutil.push_branch(clone_dir, repo, branch=branch_name(number), token=token)
    else:
        store.set_status(task.id, BLOCKED, summary=full_summary or "(blocked, no summary)")

    return WorkResult(task_id=task.id, status=status or BLOCKED, committed=committed)
