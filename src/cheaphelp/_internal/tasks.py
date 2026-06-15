"""Task state shared by the planner, workers and reviewer.

The planner produces a set of tasks for an issue. They live under the workspace
at `state/<owner>__<repo>/issue-<n>/`:

    issues.md            # the finalized spec (written by the responder)
    plan.md              # human-readable plan overview (written by the planner)
    tasks.json           # the source of truth: task list + statuses
    tasks/<id>.task.md   # human-readable per-task brief
    tasks/<id>.summary.md# worker's result write-up

`tasks.json` is authoritative; the `.md` files are readable mirrors.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cheaphelp._internal.opencode import UsageData

# Task lifecycle.
PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
BLOCKED = "blocked"


@dataclass
class Task:
    """A single unit of work produced by the planner."""

    id: str
    title: str
    details: str = ""
    depends_on: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    verify: str = ""
    status: str = PENDING
    summary: str = ""
    attempts: int = 0  # worker runs spent on this task (for timeout retry/escalation)

    @classmethod
    def from_manifest(cls, data: dict) -> Task:
        """Build a task from one entry of a planner manifest (tolerant of types)."""
        return cls(
            id=str(data.get("id") or "").strip(),
            title=str(data.get("title") or "").strip(),
            details=str(data.get("details") or "").strip(),
            depends_on=[str(d) for d in (data.get("depends_on") or [])],
            files=[str(f) for f in (data.get("files") or [])],
            verify=str(data.get("verify") or "").strip(),
        )

    def to_markdown(self) -> str:
        """Render the human-readable task brief."""
        deps = ", ".join(self.depends_on) if self.depends_on else "none"
        files = "\n".join(f"- `{f}`" for f in self.files) if self.files else "_(not specified)_"
        return (
            f"# Task {self.id}: {self.title}\n\n"
            f"- **Depends on:** {deps}\n"
            f"- **Status:** {self.status}\n\n"
            f"## Files\n{files}\n\n"
            f"## Details\n{self.details or '_(none)_'}\n\n"
            f"## Verify\n{self.verify or '_(none)_'}\n"
        )


class TaskStore:
    """Load/save the task list for one issue and drive status transitions."""

    def __init__(self, issue_dir: Path) -> None:
        self.dir = issue_dir
        self.tasks_path = issue_dir / "tasks.json"
        self.tasks_subdir = issue_dir / "tasks"

    # --- io ----------------------------------------------------------------
    def exists(self) -> bool:
        return self.tasks_path.exists()

    def load(self) -> list[Task]:
        if not self.tasks_path.exists():
            return []
        data = json.loads(self.tasks_path.read_text(encoding="utf-8"))
        return [Task(**item) for item in data.get("tasks", [])]

    def save(self, tasks: list[Task]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"tasks": [asdict(t) for t in tasks]}
        self.tasks_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def materialize(self, tasks: list[Task]) -> None:
        """Persist tasks.json and write each task's readable `.task.md` brief."""
        self.save(tasks)
        self.tasks_subdir.mkdir(parents=True, exist_ok=True)
        for task in tasks:
            (self.tasks_subdir / f"{task.id}.task.md").write_text(task.to_markdown(), encoding="utf-8")

    def write_summary(self, task_id: str, summary: str) -> Path:
        self.tasks_subdir.mkdir(parents=True, exist_ok=True)
        path = self.tasks_subdir / f"{task_id}.summary.md"
        path.write_text(summary.rstrip() + "\n", encoding="utf-8")
        return path

    # --- queries -----------------------------------------------------------
    def next_ready(self, tasks: list[Task] | None = None) -> Task | None:
        """Return the first pending task whose dependencies are all done."""
        tasks = tasks if tasks is not None else self.load()
        done_ids = {t.id for t in tasks if t.status == DONE}
        for task in tasks:
            if task.status == PENDING and all(dep in done_ids for dep in task.depends_on):
                return task
        return None

    def all_done(self, tasks: list[Task] | None = None) -> bool:
        tasks = tasks if tasks is not None else self.load()
        return bool(tasks) and all(t.status == DONE for t in tasks)

    def is_blocked(self, tasks: list[Task] | None = None) -> bool:
        """True when no task is ready to run but not everything is done."""
        tasks = tasks if tasks is not None else self.load()
        if not tasks or self.all_done(tasks):
            return False
        return self.next_ready(tasks) is None

    def set_status(self, task_id: str, status: str, *, summary: str = "") -> None:
        tasks = self.load()
        for task in tasks:
            if task.id == task_id:
                task.status = status
                if summary:
                    task.summary = summary
        self.materialize(tasks)

    def record_attempt(self, task_id: str) -> int:
        """Increment a task's attempt counter and return the new count."""
        tasks = self.load()
        count = 0
        for task in tasks:
            if task.id == task_id:
                task.attempts += 1
                count = task.attempts
        self.materialize(tasks)
        return count

    def reset_all(self) -> list[Task]:
        """Reset all tasks: set attempts to 0, flip BLOCKED -> PENDING.

        Preserves ``summary``, ``depends_on``, ``files``, ``details``,
        ``title``, and ``id`` unchanged.  Persists via ``materialize()``.
        Returns the updated task list.
        """
        tasks = self.load()
        for task in tasks:
            task.attempts = 0
            if task.status == BLOCKED:
                task.status = PENDING
        self.materialize(tasks)
        return tasks


class IssueCostStore:
    """Persistent cumulative token + cost counter for a single issue.

    ``cost.json`` stores a ``{total, by_role}`` schema::

        {
          "total": {"prompt_tokens": …, …},
          "by_role": {
            "responder": {"prompt_tokens": …, …, "count": 1},
            …
          }
        }

    Legacy flat ``UsageData`` dicts (written by an older version) are
    transparently upgraded on read.
    """

    def __init__(self, issue_dir: Path) -> None:
        self.dir = issue_dir
        self.path = issue_dir / "cost.json"

    def _read_raw(self) -> dict:
        """Return the parsed cost.json dict normalised to the new schema.

        Returns:
            A dict with ``total`` and ``by_role`` keys.  ``{}`` when the file
            is missing, unreadable, non-JSON, or not a dict.  Legacy flat
            ``UsageData`` dicts are wrapped into the new shape automatically.
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Legacy flat UsageData dict?  Wrap it.
        if "total" not in data and ("prompt_tokens" in data or "cost_usd" in data):
            return {"total": data, "by_role": {}}
        return data

    def load(self) -> UsageData:
        """Return the cumulative ``UsageData`` (total across all roles)."""
        raw = self._read_raw()
        return UsageData.from_dict(raw.get("total", {}))

    def save(self, usage: UsageData) -> None:
        """Persist a total UsageData (wipes any existing per-role data)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"total": usage.to_dict(), "by_role": {}}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def add(self, usage: UsageData, *, role: str | None = None) -> UsageData:
        """Add a turn's usage to the cumulative total, persist, return new total.

        When *role* is given, the usage is also recorded under ``by_role[role]``,
        and that role's call count is incremented.  *role* is keyword-only.
        """
        raw = self._read_raw()
        total_data = raw.get("total", {})
        total = UsageData.from_dict(total_data) + usage

        by_role = dict(raw.get("by_role", {}))
        if role is not None:
            role_data = by_role.get(role, {})
            role_usage = UsageData.from_dict(role_data) + usage
            role_entry = role_usage.to_dict()
            role_entry["count"] = int(role_data.get("count", 0)) + 1
            by_role[role] = role_entry

        payload = {"total": total.to_dict(), "by_role": by_role}
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return total

    def load_by_role(self) -> dict[str, UsageData]:
        """Return per-role cumulative ``UsageData``, keyed by role name.

        The ``count`` field that may be present in the persisted data is
        stripped — it is not a ``UsageData`` attribute.
        """
        raw = self._read_raw()
        by_role = raw.get("by_role", {})
        result: dict[str, UsageData] = {}
        for role, entry in by_role.items():
            if not isinstance(entry, dict):
                continue
            result[role] = UsageData.from_dict(entry)
        return result

    def load_role_counts(self) -> dict[str, int]:
        """Return ``{role: call_count}`` for every role that has recorded data."""
        raw = self._read_raw()
        by_role = raw.get("by_role", {})
        result: dict[str, int] = {}
        for role, entry in by_role.items():
            if isinstance(entry, dict):
                count = entry.get("count", 0)
                result[role] = int(count) if isinstance(count, (int, float)) else 0
        return result
