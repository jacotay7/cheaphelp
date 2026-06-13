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
from cheaphelp._internal.tasks import DONE, Task, TaskStore


def build_prompt(issue_md: str, *, replan_notes: str = "", existing_tasks: list[Task] | None = None) -> str:
    """Render the planner's user message from the finalized spec.

    On a re-plan (`existing_tasks` with completed work), the planner is told the
    already-done work is committed and asked for ONLY the corrective tasks.
    """
    done = [t for t in (existing_tasks or []) if t.status == DONE]
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
            "## Feedback from a previous attempt (you MUST address this)",
            "",
            replan_notes.strip(),
        ]
    if done:
        lines += [
            "",
            "## Already-completed tasks (committed on the branch — do NOT redo)",
            "",
            *[f"- `{t.id}` {t.title}" for t in done],
            "",
            "These are finished and committed. Produce ONLY new corrective tasks that "
            "address the feedback above. Use fresh ids (e.g. `fix1`, `fix2`). Reference "
            "completed task ids in `depends_on` if needed. Do NOT recreate the work above.",
        ]
    lines += [
        "",
        "---",
        "",
        "Inspect the repository in your working directory, then produce the task "
        "manifest following your output protocol (a single json block).",
    ]
    return "\n".join(lines)


def parse_manifest(decision: dict, *, known_ids: set[str] | None = None) -> tuple[str, list[Task]]:
    """Extract (plan_summary, tasks) from a planner decision, validated.

    `known_ids` are extra valid `depends_on` targets (e.g. already-done task ids
    during a re-plan). Raises ValueError if the manifest is unusable.
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
    valid_deps = set(ids) | (known_ids or set())
    for t in tasks:
        bad = [d for d in t.depends_on if d not in valid_deps]
        if bad:
            raise ValueError(f"task {t.id} depends on unknown task(s): {bad}")
    summary = str(decision.get("plan_summary") or "").strip()
    return summary, tasks


def merge_tasks(done: list[Task], new: list[Task]) -> list[Task]:
    """Append `new` corrective tasks to `done` tasks, renaming colliding ids.

    Done tasks are preserved as-is; new task ids that clash with a done id are
    renamed (and internal `depends_on` references rewritten) so nothing is lost.
    """
    used = {t.id for t in done}
    rename: dict[str, str] = {}
    for t in new:
        new_id = t.id
        i = 1
        while new_id in used:
            new_id = f"r{i}_{t.id}"
            i += 1
        rename[t.id] = new_id
        used.add(new_id)
    for t in new:
        t.id = rename[t.id]
        t.depends_on = [rename.get(d, d) for d in t.depends_on]
    return [*done, *new]


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
    """Validate a planner decision, persist tasks, and advance the issue.

    On a re-plan (existing completed tasks), the new corrective tasks are merged
    onto the done tasks rather than replacing the whole plan, so finished work is
    not redone.
    """
    issue_dir = workspace.issue_dir(owner, repo, number)
    store = TaskStore(issue_dir)
    done = [t for t in store.load() if t.status == DONE]

    try:
        summary, new_tasks = parse_manifest(decision, known_ids={t.id for t in done})
    except ValueError as exc:
        return PlanResult(number=number, error=str(exc))

    tasks = merge_tasks(done, new_tasks) if done else new_tasks
    store.materialize(tasks)
    write_plan_md(issue_dir, summary, tasks)

    verb = f"{len(new_tasks)} corrective task(s) added" if done else f"{len(tasks)} task(s)"
    body = f"{BOT_MARKER}\n\n**Plan ready — {verb}.**\n\n{summary}\n\n" + "\n".join(
        f"- `{t.id}` {t.title}" + (" ✓" if t.status == DONE else "") for t in tasks
    )
    gh.create_comment(owner, repo, number, body)

    gh.ensure_label(
        owner,
        repo,
        config.labels["planned"],
        color="1d76db",
        description="cheaphelp: planned, ready for workers",
    )
    gh.add_labels(owner, repo, number, [config.labels["planned"]])
    # Move out of the planner queue.
    gh.remove_label(owner, repo, number, config.labels["ready"])
    gh.remove_label(owner, repo, number, config.labels["needs_replan"])

    return PlanResult(number=number, task_count=len(tasks))
