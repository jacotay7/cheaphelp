"""Planner turn logic.

The planner consumes a finalized `issues.md` and produces an ordered set of small
tasks (a manifest) that workers execute one at a time. As with the responder, the
agent returns a single JSON block which we materialize into task state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import GitHubClient
from cheaphelp._internal.responder import BOT_MARKER
from cheaphelp._internal.tasks import Task, TaskStore


def build_prompt(issue_md: str, *, replan_notes: str = "") -> str:
    """Render the planner's user message from the finalized spec."""
    lines = [
        "Below is the finalized specification for the issue you must plan.",
        "",
        "## issues.md",
        "",
        issue_md.strip() or "_(empty specification)_",
    ]
    if replan_notes.strip():
        lines += [
            "",
            "## Reviewer feedback from a previous attempt (address this)",
            "",
            replan_notes.strip(),
        ]
    lines += [
        "",
        "---",
        "",
        "Inspect the repository in your working directory, then produce the task "
        "manifest following your output protocol (a single json block).",
    ]
    return "\n".join(lines)


def parse_manifest(decision: dict) -> tuple[str, list[Task]]:
    """Extract (plan_summary, tasks) from a planner decision, validated.

    Raises ValueError if the manifest is unusable.
    """
    raw_tasks = decision.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("planner produced no tasks")
    tasks = [Task.from_manifest(item) for item in raw_tasks]
    if any(not t.id or not t.title for t in tasks):
        raise ValueError("a task is missing an id or title")
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate task ids in manifest")
    known = set(ids)
    for t in tasks:
        bad = [d for d in t.depends_on if d not in known]
        if bad:
            raise ValueError(f"task {t.id} depends on unknown task(s): {bad}")
    summary = str(decision.get("plan_summary") or "").strip()
    return summary, tasks


def write_plan_md(issue_dir: Path, summary: str, tasks: list[Task]) -> Path:
    """Write the human-readable plan.md overview."""
    issue_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Implementation plan", "", summary or "_(no summary provided)_", "", "## Tasks", ""]
    for t in tasks:
        deps = f" (depends on {', '.join(t.depends_on)})" if t.depends_on else ""
        lines.append(f"- **{t.id}** — {t.title}{deps}")
    path = issue_dir / "plan.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass
class PlanResult:
    """Outcome of a planner turn, for logging."""

    number: int
    task_count: int = 0
    error: str | None = None


def apply_plan(
    gh: GitHubClient,
    workspace: Workspace,
    config: Config,
    owner: str,
    repo: str,
    number: int,
    decision: dict,
) -> PlanResult:
    """Validate a planner decision, persist tasks, and advance the issue."""
    try:
        summary, tasks = parse_manifest(decision)
    except ValueError as exc:
        return PlanResult(number=number, error=str(exc))

    issue_dir = workspace.issue_dir(owner, repo, number)
    store = TaskStore(issue_dir)
    store.materialize(tasks)
    write_plan_md(issue_dir, summary, tasks)

    body = (
        f"{BOT_MARKER}\n\n**Plan ready — {len(tasks)} task(s).**\n\n"
        f"{summary}\n\n"
        + "\n".join(f"- `{t.id}` {t.title}" for t in tasks)
    )
    gh.create_comment(owner, repo, number, body)

    gh.ensure_label(owner, repo, config.labels["planned"], color="1d76db",
                    description="cheaphelp: planned, ready for workers")
    gh.add_labels(owner, repo, number, [config.labels["planned"]])
    # Move out of the planner queue.
    gh.remove_label(owner, repo, number, config.labels["ready"])
    gh.remove_label(owner, repo, number, config.labels["needs_replan"])

    return PlanResult(number=number, task_count=len(tasks))
