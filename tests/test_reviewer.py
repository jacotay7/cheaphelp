"""Tests for cheaphelp's reviewer role."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal import (
    gitutil,
    opencode,
    planner,
    reviewer,
)
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.tasks import DONE, TaskStore


def test_reviewer_review_issue_forwards_conventions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reviewer.review_issue reads AGENTS.md from clone dir and passes it to build_prompt."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / "AGENTS.md").write_text("SENTINEL_AGENT_RULES", encoding="utf-8")

    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")
    issue_number = 1

    # Set up issue dir with issues.md and a task store with a done task.
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue_number)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Spec", encoding="utf-8")
    store = TaskStore(issue_dir)
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "Do it"}]})
    store.materialize(tasks)
    store.set_status("t1", DONE, summary="done it")

    # Mock git + opencode.
    monkeypatch.setattr(gitutil, "diff_against_base", lambda *a, **kw: ("M f.py", "diff --git a/f.py b/f.py"))
    monkeypatch.setattr(gitutil, "push_branch", lambda *a, **kw: None)

    from types import SimpleNamespace  # noqa: PLC0415

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *a, **kw: SimpleNamespace(decision={"decision": "open_pr", "pr_title": "x", "pr_body": "y"}, usage=None),
    )

    captured: list[str] = []

    def recording_build_prompt(
        issue_md: str,
        name_status: str,
        full_diff: str,
        summaries: str,
        *,
        conventions: str = "",
    ) -> str:
        captured.append(conventions)
        return "recording"

    monkeypatch.setattr(reviewer, "build_prompt", recording_build_prompt)

    # Need a fake GH that supports create_pull_request etc.
    class _FakeReviewGH:
        def create_pull_request(self, *_args: object, **_kwargs: object) -> dict:
            return {"number": 99, "html_url": "https://pr"}

        def request_reviewers(self, *_args: object, **_kwargs: object) -> None: ...
        def ensure_label(self, *_args: object, **_kwargs: object) -> None: ...
        def add_labels(self, *_args: object, **_kwargs: object) -> None: ...
        def remove_label(self, *_args: object, **_kwargs: object) -> None: ...
        def create_comment(self, *_args: object, **_kwargs: object) -> None: ...
        def authenticated_login(self) -> str:
            return "mybot"

    result = reviewer.review_issue(
        _FakeReviewGH(),  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        issue_number,
        clone_dir,
        token=None,
    )

    assert captured == ["SENTINEL_AGENT_RULES"]
    assert result.decision == "open_pr"


def test_apply_review_push_failure_routes_to_needs_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected push must not crash the build; it routes the issue to needs-human."""
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    config = Config()

    def boom(*_a: object, **_kw: object) -> None:
        raise gitutil.GitError("git push ... failed: refusing to allow a Personal Access Token")

    monkeypatch.setattr(gitutil, "push_branch", boom)

    class _RecGH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple, dict]] = []

        def __getattr__(self, name: str):
            def rec(*args: object, **kwargs: object) -> None:
                self.calls.append((name, args, kwargs))

            return rec

    gh = _RecGH()
    result = reviewer.apply_review(
        gh,  # ty: ignore[invalid-argument-type]
        ws,
        config,
        repo,
        1,
        {"decision": "open_pr"},
        tmp_path,
        token="t",
    )

    assert result.decision == "push_failed"
    assert result.error is not None
    names = [c[0] for c in gh.calls]
    # PR was never opened; the issue was labeled needs-human and dropped from planned.
    assert "create_pull_request" not in names
    add = next(c for c in gh.calls if c[0] == "add_labels")
    assert config.labels["needs_human"] in add[1][-1]
    assert "remove_label" in names
