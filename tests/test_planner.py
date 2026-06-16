"""Tests for cheaphelp's planner role."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal import (
    opencode,
    orchestrator,
    planner,
)
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.tasks import DONE, IssueCostStore


def _issue(number: int = 1, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title="t",
        body="b",
        state="open",
        labels=labels or [],
        user="alice",
        html_url="",
    )


def _comment(body: str, user: str) -> Comment:
    return Comment(id=1, body=body, user=user, created_at="")


class _RecordingResponderGH:
    """Stays at the responder stage on re-check; records any comment posted."""

    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []

    def _issue(self) -> Issue:
        return Issue(number=1, title="t", body="b", state="open", labels=[], user="human", html_url="")

    def list_open_issues(self, _owner: str, _name: str) -> list[Issue]:
        return [self._issue()]

    def list_issue_comments(self, _owner: str, _name: str, _number: int) -> list[Comment]:
        return []

    def get_issue(self, _owner: str, _name: str, _number: int) -> Issue:
        return self._issue()

    def create_comment(self, _owner: str, _name: str, number: int, body: str) -> Comment:
        self.comments.append((number, body))
        return Comment(id=1, body=body, user="mybot", created_at="")

    def authenticated_login(self) -> str:
        return "mybot"

    def ensure_label(self, *_args: object, **_kwargs: object) -> None: ...
    def add_labels(self, *_args: object, **_kwargs: object) -> None: ...
    def remove_label(self, *_args: object, **_kwargs: object) -> None: ...


# --- planner manifest ------------------------------------------------------
def test_parse_manifest_valid() -> None:
    summary, tasks = planner.parse_manifest(
        {
            "plan_summary": "do the thing",
            "tasks": [
                {"id": "t1", "title": "first", "depends_on": []},
                {"id": "t2", "title": "second", "depends_on": ["t1"]},
            ],
        },
    )
    assert summary == "do the thing"
    assert [t.id for t in tasks] == ["t1", "t2"]


@pytest.mark.parametrize(
    "decision",
    [
        {"tasks": []},  # no tasks
        {"tasks": [{"id": "t1"}]},  # missing title
        {"tasks": [{"id": "t1", "title": "a"}, {"id": "t1", "title": "b"}]},  # dup id
        {"tasks": [{"id": "t1", "title": "a", "depends_on": ["tX"]}]},  # bad dep
    ],
)
def test_parse_manifest_rejects_bad(decision: dict) -> None:
    with pytest.raises(ValueError, match=r"task|depend"):
        planner.parse_manifest(decision)


def test_parse_manifest_known_ids_allows_external_dep() -> None:
    # A corrective task may depend on an already-done task id from a prior round.
    _, tasks = planner.parse_manifest(
        {"tasks": [{"id": "fix1", "title": "fix", "depends_on": ["t2"]}]},
        known_ids={"t1", "t2"},
    )
    assert tasks[0].depends_on == ["t2"]


def test_merge_tasks_normal_case() -> None:
    done = [
        planner.Task(id="t1", title="one", status=DONE),
        planner.Task(id="t2", title="two", status=DONE),
    ]
    # Fresh corrective tasks (as the planner is instructed to produce) that
    # depend on already-done work.
    new = [
        planner.Task(id="fix1", title="fix the thing", depends_on=["t2"]),
        planner.Task(id="fix2", title="and another", depends_on=["fix1"]),
    ]
    merged = planner.merge_tasks(done, new)
    assert [t.id for t in merged] == ["t1", "t2", "fix1", "fix2"]
    assert all(t.status == DONE for t in merged[:2])  # done work preserved
    assert merged[2].depends_on == ["t2"]  # dep on done task kept
    assert merged[3].depends_on == ["fix1"]  # internal dep kept


def test_merge_tasks_renames_id_collision() -> None:
    done = [planner.Task(id="t1", title="one", status=DONE)]
    new = [planner.Task(id="t1", title="corrective")]  # reuses a done id
    merged = planner.merge_tasks(done, new)
    assert merged[0].id == "t1"  # done task untouched
    assert merged[1].id != "t1"  # new colliding task renamed
    assert merged[1].title == "corrective"


def test_planner_prompt_mentions_documentation() -> None:
    """Locks in the documentation-task guidance added to the planner prompt."""
    from cheaphelp._internal.templates import load_prompt  # noqa: PLC0415

    prompt = load_prompt("planner")
    assert "documentation" in prompt.lower()


def test_run_planner_forwards_conventions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_planner reads CHEAPHELP.md from clone dir and passes it to build_prompt."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / "CHEAPHELP.md").write_text("SENTINEL_HOUSE_RULES", encoding="utf-8")
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")

    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")
    issue = _issue(number=1)

    # Planner needs issues.md in the issue dir.
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue.number)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# The spec", encoding="utf-8")

    captured: list[str] = []

    def recording_build_prompt(
        issue_md: str,
        *,
        replan_notes: str = "",
        existing_tasks: object = None,
        conventions: str = "",
    ) -> str:
        captured.append(conventions)
        return ""

    monkeypatch.setattr(planner, "build_prompt", recording_build_prompt)

    fake_gh = _RecordingResponderGH()
    report = orchestrator.RepoReport(slug=repo.slug)

    orchestrator._run_planner(
        fake_gh,
        ws,
        Config(),
        repo,
        issue,
        clone_dir,
        lambda _m: None,
        report,
    )

    assert captured == ["SENTINEL_HOUSE_RULES"]


def test_run_planner_records_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_planner records cost from the planner agent call."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")

    repo = RepoEntry(owner="octocat", name="hello")
    issue = Issue(number=1, title="t", body="b", state="open", labels=[], user="alice", html_url="")

    # Create issues.md so planner does not early-return.
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue.number)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# The spec", encoding="utf-8")

    usage = opencode.UsageData(prompt_tokens=200, completion_tokens=100, cost_usd=0.008)
    result = opencode.AgentResult(
        returncode=0,
        stdout='```json\n{"plan_summary":"plan","tasks":[{"id":"t1","title":"x"}]}\n```',
        stderr="",
        decision={"plan_summary": "plan", "tasks": [{"id": "t1", "title": "x"}]},
        usage=usage,
    )
    monkeypatch.setattr(opencode, "run_agent", lambda *a, **kw: result)

    # Mock apply_plan so it doesn't actually modify GitHub state.
    def fake_apply_plan(*_a: object, **_kw: object) -> object:
        from cheaphelp._internal.planner import PlanResult  # noqa: PLC0415

        return PlanResult(number=1, task_count=1)

    monkeypatch.setattr(planner, "apply_plan", fake_apply_plan)

    fake_gh = _RecordingResponderGH()
    report = orchestrator.RepoReport(slug=repo.slug)

    orchestrator._run_planner(
        fake_gh,
        ws,
        Config(),
        repo,
        issue,
        tmp_path,
        lambda _m: None,
        report,
    )

    assert report.cost == usage
    assert report.issue_costs[1]["planner"] == [usage]
    cost_path = ws.issue_dir(repo.owner, repo.name, 1) / "cost.json"
    assert cost_path.exists()
    loaded = IssueCostStore(ws.issue_dir(repo.owner, repo.name, 1)).load()
    assert loaded == usage
