"""Tests for cheaphelp's PR-state persistence and rework role."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal import (
    gitutil,
    opencode,
    orchestrator,
    pr_state,
)
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.orchestrator import _process_repo, classify
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.rework import ReworkResult, run_rework


def _issue_with(labels: list[str]) -> Issue:
    return Issue(number=1, title="t", body="b", state="open", labels=labels, user="u", html_url="")


# --- pr_state ---------------------------------------------------------------
def test_pr_state_save_then_load(tmp_path: Path) -> None:
    data = {
        "pr_number": 42,
        "pr_url": "https://github.com/o/r/pull/42",
        "last_push_sha": "abc",
        "reviewers": ["alice"],
        "rework_attempts": 0,
    }
    issue_dir = tmp_path / "issue-1"
    pr_state.save_pr_state(issue_dir, data)
    loaded = pr_state.load_pr_state(issue_dir)
    assert loaded is not None
    # updated_at is auto-added; compare everything else.
    for k, v in data.items():
        assert loaded[k] == v
    assert "updated_at" in loaded


def test_pr_state_load_missing(tmp_path: Path) -> None:
    assert pr_state.load_pr_state(tmp_path / "nonexistent") is None


def test_pr_state_load_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "issue-2" / "pr_state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert pr_state.load_pr_state(tmp_path / "issue-2") is None


def test_pr_state_creates_parent_dir(tmp_path: Path) -> None:
    issue_dir = tmp_path / "a" / "b" / "issue-3"
    assert not issue_dir.exists()
    pr_state.save_pr_state(issue_dir, {"pr_number": 1})
    loaded = pr_state.load_pr_state(issue_dir)
    assert loaded is not None
    assert loaded["pr_number"] == 1


class _ReworkFakeGH:
    """Minimal GitHubClient stand-in for rework tests."""

    def __init__(self) -> None:
        self.pr_state: dict | None = None
        self.reviews: list[dict] = []
        self.comments: list = []
        self.created_comments: list[tuple[int, str]] = []
        self.requested_reviewers: list[list[str]] = []
        self.added_labels: list[list[str]] = []
        self.removed_labels: list[str] = []
        self._bot_login = "mybot"

    def authenticated_login(self) -> str:
        return self._bot_login

    def get_pull_request(self, _owner: str, _repo: str, _pr_number: int) -> dict:
        return {"state": "open", "number": _pr_number}

    def list_pr_reviews(self, _owner: str, _repo: str, _pr_number: int) -> list[dict]:
        return list(self.reviews)

    def list_pr_review_comments(self, _owner: str, _repo: str, _pr_number: int) -> list:
        return list(self.comments)

    def create_comment(self, _owner: str, _repo: str, number: int, body: str) -> None:
        self.created_comments.append((number, body))

    def request_reviewers(self, _owner: str, _repo: str, _pr_number: int, reviewers: list[str]) -> bool:
        self.requested_reviewers.append(reviewers)
        return True

    def add_labels(self, _owner: str, _repo: str, _number: int, labels: list[str]) -> None:
        self.added_labels.append(labels)

    def remove_label(self, _owner: str, _repo: str, _number: int, label: str) -> None:
        self.removed_labels.append(label)


def _make_pr_state(issue_dir: Path, **overrides: object) -> None:
    """Write a minimal pr_state.json to *issue_dir*."""
    data: dict = {
        "pr_number": 42,
        "pr_url": "https://github.com/o/r/pull/42",
        "last_push_sha": "abc123",
        "reviewers": ["alice"],
        "rework_attempts": 0,
        **overrides,  # type: ignore[arg-type]
    }
    pr_state.save_pr_state(issue_dir, data)


def test_rework_no_op_when_no_pr_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing pr_state.json -> status='no_feedback', no agent call."""
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    gh = _ReworkFakeGH()

    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: None)
    monkeypatch.setattr(opencode, "run_agent", lambda *_a, **_k: None)

    result = run_rework(gh, ws, Config(), repo, 1, token=None)  # ty: ignore[invalid-argument-type]
    assert result.status == "no_feedback"
    assert result.error == "no pr_state"


def test_rework_no_op_when_pr_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the PR is closed, return no_feedback."""
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    _make_pr_state(issue_dir)

    class _ClosedPRGH(_ReworkFakeGH):
        def get_pull_request(self, _owner: str, _repo: str, _pr_number: int) -> dict:
            return {"state": "closed", "number": _pr_number}

    gh = _ClosedPRGH()
    monkeypatch.setattr(gitutil, "_run", lambda *a, **k: "2024-01-01T00:00:00+00:00")
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: None)
    monkeypatch.setattr(opencode, "run_agent", lambda *_a, **_k: None)

    result = run_rework(gh, ws, Config(), repo, 1, token=None)  # ty: ignore[invalid-argument-type]
    assert result.status == "no_feedback"


def test_rework_no_op_when_no_new_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All reviews are APPROVED or older than last push; no agent call."""
    import datetime  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    _make_pr_state(issue_dir, last_push_sha="abc123", rework_attempts=0)

    gh = _ReworkFakeGH()
    # Make feedback timestamps much older than the "last push" time so they
    # are treated as already-addressed.
    now = datetime.datetime.now(datetime.timezone.utc)
    old_ts = (now - datetime.timedelta(hours=2)).isoformat()
    gh.reviews = [
        {"id": 1, "state": "APPROVED", "user": {"login": "alice"}, "submitted_at": old_ts},
        {"id": 2, "state": "COMMENTED", "user": {"login": "mybot"}, "submitted_at": old_ts},
    ]
    gh.comments = []

    # Make git log return a timestamp that is *after* all feedback timestamps
    # so everything is "addressed".
    later_ts = (now + datetime.timedelta(hours=1)).isoformat()
    monkeypatch.setattr(gitutil, "_run", lambda *a, **k: later_ts)
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: None)
    agent_calls: list = []
    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: agent_calls.append(True) or None,
    )

    result = run_rework(gh, ws, Config(), repo, 1, token=None)  # ty: ignore[invalid-argument-type]
    assert result.status == "no_feedback"
    assert agent_calls == []  # agent was not called


def test_rework_runs_agent_and_pushes_on_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human CHANGES_REQUESTED review -> agent runs, commits, pushes, comments."""
    import datetime  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Test spec\n\nDo the thing.\n", encoding="utf-8")
    _make_pr_state(issue_dir, last_push_sha="oldsha", rework_attempts=0)

    gh = _ReworkFakeGH()
    now = datetime.datetime.now(datetime.timezone.utc)
    new_ts = now.isoformat()
    gh.reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "alice"},
            "submitted_at": new_ts,
            "body": "Please fix the naming.",
        },
    ]
    gh.comments = []

    # git log returns an older timestamp so feedback is "new".
    old_ts = (now - datetime.timedelta(hours=2)).isoformat()
    monkeypatch.setattr(gitutil, "_run", lambda *a, **k: old_ts if "log" in str(a[0]) else "newsha456")
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(
        gitutil,
        "diff_against_base",
        lambda *_a, **_k: ("M\tsrc/main.py", "diff --git a/src/main.py b/src/main.py"),
    )
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: True)
    monkeypatch.setattr(gitutil, "rev_parse", lambda *_a, **_k: "newsha456")

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout='```json\n{"status": "done", "summary": "fixed naming", "notes": ""}\n```',
            stderr="",
            decision={"status": "done", "summary": "fixed naming", "notes": ""},
        ),
    )

    result = run_rework(gh, ws, Config(), repo, 1, token="token")  # ty: ignore[invalid-argument-type]
    assert result.status == "done"
    assert result.committed is True
    assert result.pushed is True
    assert result.summary == "fixed naming"

    # pr_state was updated with new SHA and reset attempts.
    loaded = pr_state.load_pr_state(issue_dir)
    assert loaded is not None
    assert loaded["last_push_sha"] == "newsha456"
    assert loaded["rework_attempts"] == 0

    # A PR comment was posted with the summary.
    assert len(gh.created_comments) == 1
    comment_number, comment_body = gh.created_comments[0]
    assert comment_number == 42  # PR number
    assert "fixed naming" in comment_body

    # Reviewers were re-requested.
    assert len(gh.requested_reviewers) == 1
    assert "alice" in gh.requested_reviewers[0]


def test_rework_escalates_after_max_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After max_task_attempts timeouts, the issue is escalated (needs-human)."""
    import datetime  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Test\n\nDo it.\n", encoding="utf-8")
    _make_pr_state(issue_dir, last_push_sha="sha1", rework_attempts=0)

    gh = _ReworkFakeGH()
    now = datetime.datetime.now(datetime.timezone.utc)
    new_ts = now.isoformat()
    gh.reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "alice"},
            "submitted_at": new_ts,
            "body": "Fix it.",
        },
    ]

    old_ts = (now - datetime.timedelta(hours=2)).isoformat()
    monkeypatch.setattr(gitutil, "_run", lambda *a, **k: old_ts)
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(gitutil, "diff_against_base", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="opencode", timeout=1.0)),
    )

    cfg = Config.from_dict({"max_task_attempts": 2})

    # First call: timeout -> blocked, attempt incremented to 1.
    result1 = run_rework(gh, ws, cfg, repo, 1, token=None)  # ty: ignore[invalid-argument-type]
    assert result1.status == "blocked"
    assert result1.error == "timeout"
    assert len(gh.added_labels) == 0  # not escalated yet

    # Second call: timeout -> escalated (attempt incremented to 2 >= max_task_attempts).
    result2 = run_rework(gh, ws, cfg, repo, 1, token=None)  # ty: ignore[invalid-argument-type]
    assert result2.status == "escalated"
    assert result2.error == "timeout"
    # needs-human was added, in-review was removed.
    assert any(cfg.labels["needs_human"] in labels for labels in gh.added_labels)
    assert cfg.labels["in_review"] in gh.removed_labels
    # A comment was posted to the issue explaining.
    assert len(gh.created_comments) >= 1
    assert "timed out" in gh.created_comments[-1][1].lower()


def test_rework_respects_cheaphelp_no_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With CHEAPHELP_NO_PUSH, result has pushed=False and push_branch not called."""
    import datetime  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Test\n\nDo it.\n", encoding="utf-8")
    _make_pr_state(issue_dir, last_push_sha="oldsha", rework_attempts=0)

    gh = _ReworkFakeGH()
    now = datetime.datetime.now(datetime.timezone.utc)
    new_ts = now.isoformat()
    gh.reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "alice"},
            "submitted_at": new_ts,
            "body": "Fix naming.",
        },
    ]

    old_ts = (now - datetime.timedelta(hours=2)).isoformat()
    monkeypatch.setenv("CHEAPHELP_NO_PUSH", "1")
    monkeypatch.setattr(gitutil, "_run", lambda *a, **k: old_ts if "log" in str(a[0]) else "newsha")
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(gitutil, "diff_against_base", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: True)
    monkeypatch.setattr(gitutil, "rev_parse", lambda *_a, **_k: "newsha")

    push_calls: list = []
    monkeypatch.setattr(gitutil, "push_branch", lambda *_a, **_k: push_calls.append(True))

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout='```json\n{"status": "done", "summary": "done", "notes": ""}\n```',
            stderr="",
            decision={"status": "done", "summary": "done", "notes": ""},
        ),
    )

    result = run_rework(gh, ws, Config(), repo, 1, token="token")  # ty: ignore[invalid-argument-type]
    assert result.status == "done"
    assert result.committed is True
    assert result.pushed is False
    assert push_calls == []  # push_branch was not called

    # PR comment was still posted.
    assert len(gh.created_comments) == 1
    # pr_state was updated.
    loaded = pr_state.load_pr_state(issue_dir)
    assert loaded is not None
    assert loaded["last_push_sha"] == "newsha"


def test_rework_honors_gh_no_push_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With GH_NO_PUSH, result has pushed=False and push_branch not called."""
    import datetime  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Test\n\nDo it.\n", encoding="utf-8")
    _make_pr_state(issue_dir, last_push_sha="oldsha", rework_attempts=0)

    gh = _ReworkFakeGH()
    now = datetime.datetime.now(datetime.timezone.utc)
    new_ts = now.isoformat()
    gh.reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "alice"},
            "submitted_at": new_ts,
            "body": "Fix naming.",
        },
    ]

    old_ts = (now - datetime.timedelta(hours=2)).isoformat()
    monkeypatch.setenv("GH_NO_PUSH", "1")
    monkeypatch.setattr(gitutil, "_run", lambda *a, **k: old_ts if "log" in str(a[0]) else "newsha")
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(gitutil, "diff_against_base", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: True)
    monkeypatch.setattr(gitutil, "rev_parse", lambda *_a, **_k: "newsha")

    push_calls: list = []
    monkeypatch.setattr(gitutil, "push_branch", lambda *_a, **_k: push_calls.append(True))

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout='```json\n{"status": "done", "summary": "done", "notes": ""}\n```',
            stderr="",
            decision={"status": "done", "summary": "done", "notes": ""},
        ),
    )

    result = run_rework(gh, ws, Config(), repo, 1, token="token")  # ty: ignore[invalid-argument-type]
    assert result.status == "done"
    assert result.committed is True
    assert result.pushed is False
    assert push_calls == []  # push_branch was not called

    # PR comment was still posted.
    assert len(gh.created_comments) == 1
    # pr_state was updated.
    loaded = pr_state.load_pr_state(issue_dir)
    assert loaded is not None
    assert loaded["last_push_sha"] == "newsha"


def test_rework_unparseable_escalation_comment_references_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When rework agent returns clean-exit unparseable output, the escalation comment mentions the log."""
    import datetime  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Test\n\nDo it.\n", encoding="utf-8")
    _make_pr_state(issue_dir, last_push_sha="sha1", rework_attempts=0)

    gh = _ReworkFakeGH()
    now = datetime.datetime.now(datetime.timezone.utc)
    new_ts = now.isoformat()
    gh.reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "alice"},
            "submitted_at": new_ts,
            "body": "Fix it.",
        },
    ]

    old_ts = (now - datetime.timedelta(hours=2)).isoformat()
    monkeypatch.setattr(gitutil, "_run", lambda *a, **k: old_ts)
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(gitutil, "diff_against_base", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="no json here", stderr="", decision=None),
    )

    config = Config.from_dict({"max_task_attempts": 1})
    result = run_rework(gh, ws, config, repo, 1, token=None)  # ty: ignore[invalid-argument-type]

    assert result.status == "escalated"
    assert result.error == "unparseable"
    # A comment was posted referencing the log file.
    assert len(gh.created_comments) >= 1
    last_comment = gh.created_comments[-1][1]
    assert "Escalating" in last_comment
    assert "last_unparsed_rework.log" in last_comment
    # Labels were updated.
    assert any(config.labels["needs_human"] in labels for labels in gh.added_labels)
    assert config.labels["in_review"] in gh.removed_labels


# --- rework stage integration (orchestrator dispatch) -----------------------


class _ReworkProcessRepoGH:
    """Minimal GitHub stand-in for _process_repo rework-stage tests."""

    def __init__(self, label: str) -> None:
        self._issue = Issue(number=1, title="t", body="b", state="open", labels=[label], user="human", html_url="")

    def list_open_issues(self, _owner: str, _name: str) -> list[Issue]:
        return [self._issue]

    def list_issue_comments(self, _owner: str, _name: str, _number: int) -> list[Comment]:
        return []

    def get_issue(self, _owner: str, _name: str, _number: int) -> Issue:
        return self._issue

    def authenticated_login(self) -> str:
        return "mybot"


def test_process_repo_rework_stage_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rework stage runs and counts as a turn when status is 'done'."""
    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")
    repo = RepoEntry(owner="octocat", name="hello")
    lab = Config().labels

    # Pre-write pr_state.json.
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    _make_pr_state(issue_dir, last_push_sha="abc123", rework_attempts=0)

    gh = _ReworkProcessRepoGH(lab["in_review"])

    monkeypatch.setattr(
        orchestrator.rework,
        "run_rework",
        lambda *_a, **_k: ReworkResult(number=1, status="done", committed=True, pushed=True, summary="fixed"),
    )

    report = _process_repo(
        gh,  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=False,
        log=lambda _m: None,
    )
    assert report.turns_taken == 1
    assert any("#1: rework done" in a for a in report.actions)
    assert any("+commit" in a for a in report.actions)
    assert any("+push" in a for a in report.actions)


def test_process_repo_rework_stage_no_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rework stage is a free no-op when there's no new feedback."""
    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")
    repo = RepoEntry(owner="octocat", name="hello")
    lab = Config().labels

    # Pre-write pr_state.json.
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    _make_pr_state(issue_dir, last_push_sha="abc123", rework_attempts=0)

    gh = _ReworkProcessRepoGH(lab["in_review"])

    monkeypatch.setattr(
        orchestrator.rework,
        "run_rework",
        lambda *_a, **_k: ReworkResult(number=1, status="no_feedback"),
    )

    report = _process_repo(
        gh,  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=False,
        log=lambda _m: None,
    )
    assert report.turns_taken == 0  # no_feedback is a free no-op
    assert any("#1: rework no_feedback" in a for a in report.actions)


def test_classify_in_review_returns_rework() -> None:
    """An issue with only the in_review label always classifies to rework."""
    cfg = Config()
    assert classify(_issue_with([cfg.labels["in_review"]]), [], cfg) == "rework"
