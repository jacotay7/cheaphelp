"""Tests for cheaphelp's reviewer role."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cheaphelp._internal import (
    gitutil,
    opencode,
    planner,
    pr_state,
    reviewer,
)
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.opencode import UsageData
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.tasks import DONE, IssueCostStore, TaskStore
from cheaphelp._internal.templates import load_prompt
from tests.conftest import FakeGitHubClient


def test_reviewer_prompt_mentions_documentation() -> None:
    """The reviewer prompt includes a documentation check in its checklist."""
    prompt = load_prompt("reviewer")
    assert "Documentation:**" in prompt


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


# ---------------------------------------------------------------------------
# _format_cost_table unit tests
# ---------------------------------------------------------------------------


def test_format_cost_table_returns_empty_for_missing_cost_file(tmp_path: Path) -> None:
    r"""_format_cost_table returns ``""`` when no cost.json exists."""
    issue_dir = tmp_path / "issue-1"
    issue_dir.mkdir(parents=True)
    assert reviewer._format_cost_table(issue_dir) == ""


def test_format_cost_table_returns_empty_for_all_zero(tmp_path: Path) -> None:
    r"""_format_cost_table returns ``""`` when the cumulative total is all-zero."""
    issue_dir = tmp_path / "issue-1"
    issue_dir.mkdir(parents=True)
    IssueCostStore(issue_dir).save(UsageData())
    assert reviewer._format_cost_table(issue_dir) == ""


def test_format_cost_table_returns_empty_when_no_per_role_data(tmp_path: Path) -> None:
    r"""_format_cost_table returns ``""`` when by_role is empty (even with non-zero total)."""
    issue_dir = tmp_path / "issue-1"
    issue_dir.mkdir(parents=True)
    # Write a cost.json with a non-zero total but empty by_role.
    payload = {
        "total": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.005},
        "by_role": {},
    }
    (issue_dir / "cost.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert reviewer._format_cost_table(issue_dir) == ""


def test_format_cost_table_renders_full_breakdown(tmp_path: Path) -> None:
    """_format_cost_table returns both lines with correct ordering and formatting."""
    issue_dir = tmp_path / "issue-1"
    issue_dir.mkdir(parents=True)
    store = IssueCostStore(issue_dir)
    store.add(UsageData(prompt_tokens=100, completion_tokens=50, total_tokens=150, cost_usd=0.005), role="responder")
    store.add(UsageData(prompt_tokens=400, completion_tokens=200, total_tokens=600, cost_usd=0.012), role="planner")
    store.add(UsageData(prompt_tokens=300, completion_tokens=150, total_tokens=450, cost_usd=0.011), role="worker")
    store.add(UsageData(prompt_tokens=300, completion_tokens=150, total_tokens=450, cost_usd=0.011), role="worker")
    store.add(UsageData(prompt_tokens=200, completion_tokens=100, total_tokens=300, cost_usd=0.006), role="reviewer")
    store.add(UsageData(prompt_tokens=50, completion_tokens=25, total_tokens=75, cost_usd=0.002), role="rework")

    result = reviewer._format_cost_table(issue_dir)
    assert result != ""
    lines = result.split("\n")
    assert len(lines) == 2

    # Check total line.
    total_line = lines[0]
    assert total_line.startswith("**Cost:**")
    assert "$0.047" in total_line  # 0.005 + 0.012 + 0.022 + 0.006 + 0.002
    assert "1,350 prompt" in total_line  # 100 + 400 + 600 + 200 + 50
    assert "675 completion" in total_line  # 50 + 200 + 300 + 100 + 25

    # Check per-role line.
    per_role_line = lines[1]
    assert per_role_line.startswith("**Per role:**")
    # Fixed order first: responder, planner, worker, reviewer
    assert "responder $0.005" in per_role_line
    assert "planner $0.012" in per_role_line
    assert "worker x2 $0.022" in per_role_line  # count > 1
    assert "reviewer $0.006" in per_role_line
    # Unknown role (rework) sorts after the fixed order.
    assert "rework $0.002" in per_role_line
    # Verify ordering: responder before planner before worker before reviewer before rework.
    assert per_role_line.index("responder") < per_role_line.index("planner")
    assert per_role_line.index("planner") < per_role_line.index("worker")
    assert per_role_line.index("worker") < per_role_line.index("reviewer")
    assert per_role_line.index("reviewer") < per_role_line.index("rework")


def test_format_cost_table_tolerates_corrupt_cost_file(tmp_path: Path) -> None:
    r"""_format_cost_table returns ``""`` when cost.json is not valid JSON."""
    issue_dir = tmp_path / "issue-1"
    issue_dir.mkdir(parents=True)
    (issue_dir / "cost.json").write_text("this is not json", encoding="utf-8")
    assert reviewer._format_cost_table(issue_dir) == ""


# ---------------------------------------------------------------------------
# apply_review integration tests — cost table injection
# ---------------------------------------------------------------------------


def test_apply_review_injects_cost_table_into_pr_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_review includes the cost section in the PR body when cost data exists."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")
    issue_number = 1
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue_number)
    issue_dir.mkdir(parents=True, exist_ok=True)

    # Seed cost data via IssueCostStore.
    store = IssueCostStore(issue_dir)
    store.add(UsageData(prompt_tokens=100, completion_tokens=50, total_tokens=150, cost_usd=0.005), role="responder")
    store.add(UsageData(prompt_tokens=200, completion_tokens=100, total_tokens=300, cost_usd=0.010), role="planner")
    store.add(UsageData(prompt_tokens=300, completion_tokens=150, total_tokens=450, cost_usd=0.015), role="worker")
    store.add(UsageData(prompt_tokens=150, completion_tokens=75, total_tokens=225, cost_usd=0.007), role="reviewer")

    # Patch git and pr_state.
    monkeypatch.setattr(gitutil, "push_branch", lambda *a, **kw: None)
    monkeypatch.setattr(gitutil, "rev_parse", lambda *a, **kw: "abc123")
    monkeypatch.setattr(pr_state, "save_pr_state", lambda *a, **kw: None)

    gh = FakeGitHubClient()
    config = Config()
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    result = reviewer.apply_review(
        gh,
        ws,
        config,
        repo,
        issue_number,
        {"decision": "open_pr", "pr_title": "Fix stuff", "pr_body": "Here is the fix."},
        clone_dir,
        token="t",
    )

    assert result.decision == "open_pr"
    assert len(gh.created_prs) == 1
    pr = gh.created_prs[0]
    body: str = pr["body"]

    # The cost section is present.
    assert "**Cost:**" in body
    assert "**Per role:**" in body
    # The standard PR body elements are present.
    assert "Closes #1" in body
    assert "_Opened by cheaphelp; awaiting human review._" in body
    # The hidden marker is present (cheaphelp_message wraps the whole thing).
    assert "<!-- cheaphelp:responder -->" in body

    # The cost section appears before the "Opened by cheaphelp" line.
    cost_pos = body.index("**Cost:**")
    footer_pos = body.index("_Opened by cheaphelp; awaiting human review._")
    assert cost_pos < footer_pos


def test_apply_review_omits_cost_section_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_review omits the cost section when no cost.json exists."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")
    issue_number = 1
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue_number)
    issue_dir.mkdir(parents=True, exist_ok=True)
    # Do NOT seed cost.json.

    monkeypatch.setattr(gitutil, "push_branch", lambda *a, **kw: None)
    monkeypatch.setattr(gitutil, "rev_parse", lambda *a, **kw: "abc123")
    monkeypatch.setattr(pr_state, "save_pr_state", lambda *a, **kw: None)

    gh = FakeGitHubClient()
    config = Config()
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    result = reviewer.apply_review(
        gh,
        ws,
        config,
        repo,
        issue_number,
        {"decision": "open_pr", "pr_title": "Fix", "pr_body": "Body"},
        clone_dir,
        token="t",
    )

    assert result.decision == "open_pr"
    assert len(gh.created_prs) == 1
    body: str = gh.created_prs[0]["body"]

    assert "**Cost:**" not in body
    assert "**Per role:**" not in body
    assert "Closes #1" in body
    assert "_Opened by cheaphelp; awaiting human review._" in body


def test_apply_review_omits_cost_section_when_all_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_review omits the cost section when cost data is all-zero."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")
    issue_number = 1
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue_number)
    issue_dir.mkdir(parents=True, exist_ok=True)

    # Seed all-zero cost data.
    IssueCostStore(issue_dir).save(UsageData())

    monkeypatch.setattr(gitutil, "push_branch", lambda *a, **kw: None)
    monkeypatch.setattr(gitutil, "rev_parse", lambda *a, **kw: "abc123")
    monkeypatch.setattr(pr_state, "save_pr_state", lambda *a, **kw: None)

    gh = FakeGitHubClient()
    config = Config()
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    result = reviewer.apply_review(
        gh,
        ws,
        config,
        repo,
        issue_number,
        {"decision": "open_pr", "pr_title": "Fix", "pr_body": "Body"},
        clone_dir,
        token="t",
    )

    assert result.decision == "open_pr"
    assert len(gh.created_prs) == 1
    body: str = gh.created_prs[0]["body"]

    assert "**Cost:**" not in body
    assert "**Per role:**" not in body
    assert "Closes #1" in body
    assert "_Opened by cheaphelp; awaiting human review._" in body
