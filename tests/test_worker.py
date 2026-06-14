"""Tests for cheaphelp's worker role."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from cheaphelp._internal import (
    gitutil,
    opencode,
    orchestrator,
    planner,
    reviewer,
    worker,
)
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import Issue
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.spend import DailySpendTracker
from cheaphelp._internal.tasks import BLOCKED, DONE, PENDING, TaskStore


class _RecBuildGH:
    """GitHub stand-in that records every label/comment call made during build."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def ensure_label(self, *a: object, **_kwargs: object) -> None:
        self.calls.append(("ensure_label", a))

    def add_labels(self, *a: object, **_kwargs: object) -> None:
        self.calls.append(("add_labels", a))

    def remove_label(self, *a: object, **_kwargs: object) -> None:
        self.calls.append(("remove_label", a))

    def create_comment(self, *a: object, **_kwargs: object) -> None:
        self.calls.append(("create_comment", a))


def test_run_task_timeout_retries_then_escalates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess  # noqa: PLC0415 - local to keep the module import list lean

    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="o", name="r")
    store = TaskStore(ws.issue_dir(repo.owner, repo.name, 1))
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "x"}]})
    store.materialize(tasks)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=1.0)

    monkeypatch.setattr(opencode, "run_agent", boom)
    cfg = Config.from_dict({"max_task_attempts": 2})

    # First timeout: reset to pending and retry next tick (not escalated yet).
    res1 = worker.run_task(ws, cfg, repo, 1, store.load()[0], tmp_path, token=None)
    assert res1.status == "timeout"
    assert store.load()[0].status == PENDING
    assert store.load()[0].attempts == 1

    # Second timeout hits the limit -> blocked (the orchestrator labels needs-human).
    res2 = worker.run_task(ws, cfg, repo, 1, store.load()[0], tmp_path, token=None)
    assert res2.status == BLOCKED
    assert store.load()[0].status == BLOCKED
    assert store.load()[0].attempts == 2


def test_worker_branch_name() -> None:
    assert worker.branch_name(42) == "cheaphelp/issue-42"


def test_worker_build_prompt_is_task_scoped_without_full_gate() -> None:
    task = worker.Task(id="t1", title="Do the thing")
    prompt = worker.build_prompt(task, "the spec")
    assert "Do the thing" in prompt
    assert "the spec" in prompt
    # The worker is not handed the full quality gate to run per task; that runs
    # once at the review step in the orchestrator.
    assert "Quality gate" not in prompt


def test_run_build_blast_radius_prevents_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When blast radius triggers, the reviewer is never called."""
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue = Issue(number=1, title="t", body="b", state="open", labels=[], user="u", html_url="")
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue.number)
    store = TaskStore(issue_dir)
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "one"}]})
    store.materialize(tasks)

    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: None)

    def fake_run_task(_ws, _cfg, _repo, number, task, _work_dir, *, token):  # noqa: ANN001, ANN202
        TaskStore(ws.issue_dir(repo.owner, repo.name, number)).set_status(task.id, DONE, summary="ok")
        return worker.WorkResult(task_id=task.id, status=DONE, committed=True)

    monkeypatch.setattr(worker, "run_task", fake_run_task)

    # Stub diff_stat to trigger the blast radius.
    monkeypatch.setattr(gitutil, "diff_stat", lambda _w, _r: (45, 10, 5))

    reviewer_called: list[str] = []

    def fake_review_issue(*_a: object, **_kw: object) -> object:
        reviewer_called.append("called")
        from cheaphelp._internal.reviewer import ReviewResult  # noqa: PLC0415

        return ReviewResult(number=1, decision="open-pr")

    monkeypatch.setattr(reviewer, "review_issue", fake_review_issue)

    gh = _RecBuildGH()
    report = orchestrator.RepoReport(slug=repo.slug)
    config = Config()
    orchestrator._run_build(
        gh,
        ws,
        config,
        repo,
        issue,
        "token",
        lambda _m: None,
        report,
    )

    # The reviewer must not have been called.
    assert reviewer_called == [], "reviewer was called despite blast-radius trigger"

    # needs-human label was added.
    add_labels_calls = [c for c in gh.calls if c[0] == "add_labels"]
    needs_human_added = any(config.labels["needs_human"] in cast("list[str]", c[1][-1]) for c in add_labels_calls)
    assert needs_human_added, "needs-human label should have been added"


def test_worker_run_task_forwards_conventions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker.run_task reads AGENTS.md from clone dir and passes it to build_prompt."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / "AGENTS.md").write_text("SENTINEL_AGENT_RULES", encoding="utf-8")

    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")
    issue_number = 1

    # Set up task store with a task.
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue_number)
    store = TaskStore(issue_dir)
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "Do something"}]})
    store.materialize(tasks)

    # Mock git + opencode so the function doesn't actually run anything.
    monkeypatch.setattr(gitutil, "commit_all", lambda *a, **kw: True)
    monkeypatch.setattr(gitutil, "push_branch", lambda *a, **kw: None)

    from types import SimpleNamespace  # noqa: PLC0415

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *a, **kw: SimpleNamespace(decision={"status": "done", "summary": "ok"}, usage=None),
    )

    captured: list[str] = []

    def recording_build_prompt(
        task: object,
        issue_md: str,
        *,
        conventions: str = "",
    ) -> str:
        captured.append(conventions)
        return "recording"

    monkeypatch.setattr(worker, "build_prompt", recording_build_prompt)

    result = worker.run_task(ws, Config(), repo, issue_number, store.load()[0], clone_dir, token=None)

    assert captured == ["SENTINEL_AGENT_RULES"]
    assert result.status == "done"


def test_run_build_records_worker_and_reviewer_costs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_build records costs for each worker turn and the reviewer."""
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue = Issue(number=1, title="t", body="b", state="open", labels=[], user="u", html_url="")
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue.number)
    store = TaskStore(issue_dir)
    _, tasks = planner.parse_manifest(
        {"tasks": [{"id": "t1", "title": "one"}, {"id": "t2", "title": "two"}]},
    )
    store.materialize(tasks)

    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: None)
    monkeypatch.setattr(gitutil, "diff_stat", lambda *_a, **_k: None)
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: False)
    monkeypatch.setattr(gitutil, "push_branch", lambda *_a, **_k: None)

    worker_usage = opencode.UsageData(prompt_tokens=50, completion_tokens=25, cost_usd=0.001)

    def fake_run_task(_ws, _cfg, _repo, number, task, _work_dir, *, token):  # noqa: ANN001, ANN202
        TaskStore(ws.issue_dir(repo.owner, repo.name, number)).set_status(task.id, DONE, summary="ok")
        return worker.WorkResult(task_id=task.id, status=DONE, committed=True, usage=worker_usage)

    monkeypatch.setattr(worker, "run_task", fake_run_task)

    reviewer_usage = opencode.UsageData(prompt_tokens=30, completion_tokens=15, cost_usd=0.002)

    def fake_review_issue(*_a: object, **_kw: object) -> object:
        return reviewer.ReviewResult(number=1, decision="open_pr", usage=reviewer_usage)

    monkeypatch.setattr(reviewer, "review_issue", fake_review_issue)

    gh = _RecBuildGH()
    report = orchestrator.RepoReport(slug=repo.slug)
    config = Config()
    orchestrator._run_build(gh, ws, config, repo, issue, "token", lambda _m: None, report)

    # Two workers + one reviewer.
    expected_cost = worker_usage + worker_usage + reviewer_usage
    assert report.cost == expected_cost
    assert len(report.issue_costs[1]["worker"]) == 2
    assert report.issue_costs[1]["worker"][0] == worker_usage
    assert report.issue_costs[1]["worker"][1] == worker_usage
    assert report.issue_costs[1]["reviewer"] == [reviewer_usage]


def test_run_build_aborts_before_reviewer_when_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the worker runs, the tracker reports spend >= cap, so the reviewer must NOT be called."""
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue = Issue(number=1, title="t", body="b", state="open", labels=[], user="u", html_url="")
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue.number)
    store = TaskStore(issue_dir)
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "one"}]})
    store.materialize(tasks)

    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: None)
    monkeypatch.setattr(gitutil, "diff_stat", lambda *_a, **_k: None)
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: False)
    monkeypatch.setattr(gitutil, "push_branch", lambda *_a, **_k: None)

    # Worker usage = 0.005, cap = 0.001 => after worker runs, cap is exceeded.
    worker_usage = opencode.UsageData(cost_usd=0.005)

    def fake_run_task(_ws, _cfg, _repo, number, task, _work_dir, *, token):  # noqa: ANN001, ANN202
        store.set_status(task.id, DONE, summary="ok")
        return worker.WorkResult(task_id=task.id, status=DONE, committed=True, usage=worker_usage)

    monkeypatch.setattr(worker, "run_task", fake_run_task)

    reviewer_called: list[str] = []

    def fake_review_issue(*_a: object, **_kw: object) -> object:
        reviewer_called.append("called")
        return reviewer.ReviewResult(number=1, decision="open_pr")

    monkeypatch.setattr(reviewer, "review_issue", fake_review_issue)

    gh = _RecBuildGH()
    report = orchestrator.RepoReport(slug=repo.slug)
    tracker = DailySpendTracker(ws.state_dir)
    cfg = Config.from_dict({"daily_budget_usd": 0.001})

    orchestrator._run_build(gh, ws, cfg, repo, issue, "token", lambda _m: None, report, tracker=tracker)

    # Reviewer must NOT have been called.
    assert reviewer_called == [], "reviewer was called despite budget exhaustion"

    # A "budget-exceeded" action was appended (from the mid-build or post-worker budget check).
    budget_actions = [a for a in report.actions if "budget-exceeded" in a]
    assert len(budget_actions) >= 1

    assert report.budget_exhausted is True
