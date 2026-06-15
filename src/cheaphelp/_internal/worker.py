"""Worker turn logic.

A worker executes a single task in the issue's persistent work clone, on the
issue branch. The agent edits files; this module handles git (commit), records a
summary, and advances task state. One task is run per call.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal import gitutil, opencode
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.conventions import read_conventions
from cheaphelp._internal.opencode import UsageData
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.tasks import BLOCKED, DONE, PENDING, Task, TaskStore

TIMEOUT = "timeout"  # WorkResult status for a retryable agent timeout


def branch_name(number: int) -> str:
    """The working branch for an issue."""
    return f"cheaphelp/issue-{number}"


def build_prompt(task: Task, issue_md: str, *, conventions: str = "") -> str:
    """Render the worker's user message for a single task.

    The worker implements and lightly verifies one task; it does NOT run the
    repo's full quality gate. That gate (`autofix`/`checks`) runs once,
    deterministically, in the orchestrator before the reviewer — so the full
    (and possibly expensive, e.g. multi-version) check is not repeated after
    every task.
    """
    lines = [
        "You are implementing ONE task that is part of a larger issue.",
        "",
        "## Issue context (for background only — do not implement the whole issue)",
        "",
        issue_md.strip() or "_(no spec)_",
        "",
        "## Your task",
        "",
        task.to_markdown(),
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
        "Implement this task in the working directory, verify it, then report "
        "following your output protocol (a single json block).",
    ]
    return "\n".join(lines)


@dataclass
class WorkResult:
    """Outcome of a worker turn, for logging."""

    task_id: str
    status: str
    committed: bool = False
    error: str | None = None
    usage: UsageData | None = None


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
    issue_dir = workspace.issue_dir(repo.owner, repo.name, number)
    store = TaskStore(issue_dir)
    issue_md_path = issue_dir / "issues.md"
    issue_md = issue_md_path.read_text(encoding="utf-8") if issue_md_path.exists() else ""

    store.set_status(task.id, "in_progress")
    conventions = read_conventions(clone_dir)
    prompt = build_prompt(task, issue_md, conventions=conventions)
    usage: UsageData | None = None
    try:
        result = opencode.run_agent(
            workspace, config, "worker", prompt, cwd=clone_dir, timeout=config.agent_timeout, issue_dir=issue_dir,
        )
        usage = result.usage
    except subprocess.TimeoutExpired:
        # A timeout is retryable: reset to pending and let the next tick try
        # again, escalating to blocked (-> needs-human) only after the limit.
        attempts = store.record_attempt(task.id)
        if attempts >= config.max_task_attempts:
            store.set_status(task.id, BLOCKED, summary=f"Worker timed out after {attempts} attempt(s).")
            return WorkResult(task_id=task.id, status=BLOCKED, error="timeout")
        store.set_status(task.id, PENDING)
        return WorkResult(task_id=task.id, status=TIMEOUT, error="timeout")

    decision = result.decision or {}
    status = str(decision.get("status", "")).strip().lower()
    summary = str(decision.get("summary", "")).strip()
    notes = str(decision.get("notes", "")).strip()

    if result.decision is None:
        # No parseable decision: leave whatever changes exist but mark blocked.
        store.set_status(
            task.id, BLOCKED, summary="Worker produced no parseable result (see `last_unparsed_worker.log`).",
        )
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

    return WorkResult(task_id=task.id, status=status or BLOCKED, committed=committed, usage=usage)
