"""Tests for cheaphelp's core logic that does not require network access."""

from __future__ import annotations

import json
import os
import random
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx
import pytest

from cheaphelp._internal import (
    cleanup,
    gitutil,
    opencode,
    orchestrator,
    planner,
    pr_state,
    responder,
    reviewer,
    systemd,
    worker,
)
from cheaphelp._internal.config import DEFAULT_AGENT_TIMEOUT, DEFAULT_MODELS, Config, Workspace
from cheaphelp._internal.conventions import CONVENTIONS_FILES, read_conventions
from cheaphelp._internal.env import parse_env, read_env_file, update_env_file
from cheaphelp._internal.github import Comment, GitHubClient, GitHubError, Issue, PRReviewComment
from cheaphelp._internal.lock import RunLock
from cheaphelp._internal.orchestrator import _process_repo, _short_exc, classify, parse_depends_on
from cheaphelp._internal.registry import Registry, RepoEntry, parse_slug
from cheaphelp._internal.responder import (
    ATTRIBUTION_PREFIX,
    BOT_MARKER,
    attribution_header,
    build_prompt,
    cheaphelp_message,
    is_bot_comment,
    needs_turn,
)
from cheaphelp._internal.tasks import BLOCKED, DONE, PENDING, IssueCostStore, Task, TaskStore


# --- config / workspace ----------------------------------------------------
def test_workspace_roundtrip(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    assert not ws.exists()
    ws.ensure()
    ws.save_config(Config())
    assert ws.exists()
    loaded = ws.load_config()
    assert loaded.models == DEFAULT_MODELS
    assert loaded.model_for("responder") == DEFAULT_MODELS["responder"]


def test_config_merges_defaults() -> None:
    cfg = Config.from_dict({"models": {"worker": "openrouter/custom"}})
    assert cfg.model_for("worker") == "openrouter/custom"
    # Missing roles fall back to defaults.
    assert cfg.model_for("responder") == DEFAULT_MODELS["responder"]


def test_agent_timeout_default_and_roundtrip() -> None:
    # Default when constructed with no args.
    assert Config().agent_timeout == DEFAULT_AGENT_TIMEOUT == 600.0
    # Default when the key is absent from the on-disk dict.
    assert Config.from_dict({}).agent_timeout == 600.0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"agent_timeout": 1200})
    assert cfg.agent_timeout == 1200.0
    assert Config.from_dict(cfg.to_dict()).agent_timeout == 1200.0
    # Float-typed values are accepted (the spec says `float`).
    assert Config.from_dict({"agent_timeout": 30.5}).agent_timeout == 30.5


def test_max_issues_per_tick_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().max_issues_per_tick == 0
    assert Config.from_dict({}).max_issues_per_tick == 0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"max_issues_per_tick": 5})
    assert cfg.max_issues_per_tick == 5
    assert Config.from_dict(cfg.to_dict()).max_issues_per_tick == 5
    # String values are coerced via int(...).
    assert Config.from_dict({"max_issues_per_tick": "7"}).max_issues_per_tick == 7


def test_max_tasks_per_tick_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().max_tasks_per_tick == 0
    assert Config.from_dict({}).max_tasks_per_tick == 0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"max_tasks_per_tick": 3})
    assert cfg.max_tasks_per_tick == 3
    assert Config.from_dict(cfg.to_dict()).max_tasks_per_tick == 3
    # String values are coerced via int(...).
    assert Config.from_dict({"max_tasks_per_tick": "2"}).max_tasks_per_tick == 2


def test_max_task_attempts_default_and_roundtrip() -> None:
    # Defaults to 2 (one retry) when unset.
    assert Config().max_task_attempts == 2
    assert Config.from_dict({}).max_task_attempts == 2
    cfg = Config.from_dict({"max_task_attempts": 4})
    assert cfg.max_task_attempts == 4
    assert Config.from_dict(cfg.to_dict()).max_task_attempts == 4


def test_prune_work_clones_default_and_roundtrip() -> None:
    assert Config().prune_work_clones is True
    assert Config.from_dict({}).prune_work_clones is True
    cfg = Config.from_dict({"prune_work_clones": False})
    assert cfg.prune_work_clones is False
    assert Config.from_dict(cfg.to_dict()).prune_work_clones is False


def test_retry_attempts_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().retry_attempts == 3
    assert Config.from_dict({}).retry_attempts == 3
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"retry_attempts": 5})
    assert cfg.retry_attempts == 5
    assert Config.from_dict(cfg.to_dict()).retry_attempts == 5
    # String values are coerced via int(...).
    assert Config.from_dict({"retry_attempts": "4"}).retry_attempts == 4


def test_retry_base_delay_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().retry_base_delay == 1.0
    assert Config.from_dict({}).retry_base_delay == 1.0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"retry_base_delay": 2.5})
    assert cfg.retry_base_delay == 2.5
    assert Config.from_dict(cfg.to_dict()).retry_base_delay == 2.5
    # String values are coerced via float(...).
    assert Config.from_dict({"retry_base_delay": "0.5"}).retry_base_delay == 0.5


def test_config_daily_budget_usd_default_and_roundtrip() -> None:
    # Default when constructed with no args.
    assert Config().daily_budget_usd == 0.0
    # Default when the key is absent from the on-disk dict.
    assert Config.from_dict({}).daily_budget_usd == 0.0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"daily_budget_usd": 5.0})
    assert cfg.daily_budget_usd == 5.0
    assert Config.from_dict(cfg.to_dict()).daily_budget_usd == 5.0
    # String values are coerced via float(...).
    assert Config.from_dict({"daily_budget_usd": "2.5"}).daily_budget_usd == 2.5


def test_config_budget_warn_at_default_and_roundtrip() -> None:
    # Default when constructed with no args.
    assert Config().budget_warn_at == 0.80
    # Default when the key is absent from the on-disk dict.
    assert Config.from_dict({}).budget_warn_at == 0.80
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"budget_warn_at": 0.5})
    assert cfg.budget_warn_at == 0.5
    assert Config.from_dict(cfg.to_dict()).budget_warn_at == 0.5
    # String values are coerced via float(...).
    assert Config.from_dict({"budget_warn_at": "0.9"}).budget_warn_at == 0.9


def _make_github_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    root: str = "https://api.github.com",
    timeout: float = 30.0,
    retry_attempts: int = 3,
    retry_base_delay: float = 1.0,
) -> GitHubClient:
    """Build a GitHubClient wired to an httpx.MockTransport (no network)."""
    client = httpx.Client(base_url=root, transport=httpx.MockTransport(handler))
    return GitHubClient(
        "test-token",
        root=root,
        timeout=timeout,
        retry_attempts=retry_attempts,
        retry_base_delay=retry_base_delay,
        client=client,
    )


# --- GitHubClient retry tests -----------------------------------------------
def _make_counting_handler() -> tuple[list[httpx.Response], Callable]:
    """Return (responses, handler) — the handler pops from *responses* each call.

    Pop an ``httpx.Response`` to return, or raise the item if it is an exception class.
    """
    items: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return items.pop(0)

    return items, handler


def test_request_retries_on_5xx_then_succeeds() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(503))
    items.append(httpx.Response(503))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(items) == 0  # all consumed


def test_request_retries_on_429_then_succeeds() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(429))
    items.append(httpx.Response(429))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(items) == 0


def test_request_does_not_retry_on_404() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(404))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(GitHubError) as exc_info:
        gh._request("GET", "/test")
    assert "404" in str(exc_info.value)
    assert len(items) == 0


def test_request_does_not_retry_on_422() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(422))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(GitHubError) as exc_info:
        gh._request("GET", "/test")
    assert "422" in str(exc_info.value)
    assert len(items) == 0


def test_request_retries_on_connect_error() -> None:
    calls: list[int] = []

    def handler_connect(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"ok": True})

    gh = _make_github_client(handler_connect, retry_attempts=3, retry_base_delay=0.01)
    result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(calls) == 3


def test_request_exhausted_retries_raises_last_error() -> None:
    items, handler = _make_counting_handler()
    for _ in range(4):  # one more than needed
        items.append(httpx.Response(503))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(GitHubError) as exc_info:
        gh._request("GET", "/test")
    assert "503" in str(exc_info.value)


def test_request_exhausted_transport_raises_last_error() -> None:
    calls: list[int] = []

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("timed out")

    gh = _make_github_client(handler_timeout, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(httpx.ReadTimeout):
        gh._request("GET", "/test")
    assert len(calls) == 3


def test_request_honors_retry_after() -> None:
    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    items.append(httpx.Response(429, headers={"Retry-After": "5"}))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.0)

    with patch("time.sleep", fake_sleep):
        result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(sleeps) == 1
    assert sleeps[0] >= 5.0


def test_request_honors_retry_after_caps_at_60() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    items.append(httpx.Response(429, headers={"Retry-After": "999"}))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.0)

    with patch("time.sleep", fake_sleep):
        result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(sleeps) == 1
    assert sleeps[0] <= 60.0


def test_request_no_sleep_on_last_attempt() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    for _ in range(3):
        items.append(httpx.Response(503))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)

    with patch("time.sleep", fake_sleep), pytest.raises(GitHubError):
        gh._request("GET", "/test")
    assert len(sleeps) == 2  # retry_attempts - 1


def test_request_backoff_is_exponential() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    random.seed(42)  # deterministic jitter
    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    for _ in range(3):
        items.append(httpx.Response(503))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=1.0)

    with patch("time.sleep", fake_sleep), pytest.raises(GitHubError):
        gh._request("GET", "/test")
    assert len(sleeps) == 2

    # Attempt 1: base * 2^(0) = 1.0, jitter ±0.25
    assert 0.75 <= sleeps[0] <= 1.25
    # Attempt 2: base * 2^(1) = 2.0, jitter ±0.5
    assert 1.5 <= sleeps[1] <= 2.5


def _make_clone(ws: Workspace, name: str) -> Path:
    """Create a fake clone directory (with a .git marker) under the workspace."""
    path = ws.clones_dir / name
    (path / ".git").mkdir(parents=True)
    return path


def test_iter_issue_work_clones(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    _make_clone(ws, "octocat__hello")  # shared clone: excluded
    _make_clone(ws, "octocat__hello__issue-1")
    _make_clone(ws, "octocat__hello__issue-12")
    _make_clone(ws, "octocat__other__issue-3")  # different repo: excluded
    assert set(cleanup.iter_issue_work_clones(ws, "octocat", "hello")) == {1, 12}


def test_prune_repo_work_clones_removes_closed_keeps_live(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    for n in (1, 2, 3):
        _make_clone(ws, f"octocat__hello__issue-{n}")

    removed = cleanup.prune_repo_work_clones(ws, repo, {2})  # only #2 is still open
    assert sorted(removed) == [1, 3]
    assert not (ws.clones_dir / "octocat__hello__issue-1").exists()
    assert (ws.clones_dir / "octocat__hello__issue-2").exists()
    assert not (ws.clones_dir / "octocat__hello__issue-3").exists()


def test_prune_repo_work_clones_dry_run_keeps_files(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    _make_clone(ws, "octocat__hello__issue-9")
    removed = cleanup.prune_repo_work_clones(ws, repo, set(), dry_run=True)
    assert removed == [9]
    assert (ws.clones_dir / "octocat__hello__issue-9").exists()  # reported, not deleted


def test_prune_repo_work_clones_skips_locked(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    _make_clone(ws, "octocat__hello__issue-5")
    # A concurrent tick holds the issue lock -> pruning leaves it alone.
    with RunLock(ws.issue_lock_path("octocat", "hello", 5)) as held:
        assert held.acquired
        removed = cleanup.prune_repo_work_clones(ws, repo, set())
    assert removed == []
    assert (ws.clones_dir / "octocat__hello__issue-5").exists()


def test_prune_orphan_clones(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    _make_clone(ws, "octocat__hello")
    _make_clone(ws, "octocat__hello__issue-1")
    _make_clone(ws, "ghost__repo")
    _make_clone(ws, "ghost__repo__issue-2")
    removed = cleanup.prune_orphan_clones(ws, [RepoEntry(owner="octocat", name="hello")])
    assert set(removed) == {"ghost__repo", "ghost__repo__issue-2"}
    assert (ws.clones_dir / "octocat__hello").exists()  # registered: kept
    assert (ws.clones_dir / "octocat__hello__issue-1").exists()


class _FakeGitHub:
    """Minimal stand-in for GitHubClient used by _process_repo tests."""

    def __init__(self, issue_count: int) -> None:
        self._issues = [
            Issue(number=n, title="t", body="b", state="open", labels=[], user="human", html_url="")
            for n in range(1, issue_count + 1)
        ]

    def list_open_issues(self, _owner: str, _name: str) -> list[Issue]:
        return self._issues

    def list_issue_comments(self, _owner: str, _name: str, _number: int) -> list[Comment]:
        return []

    def authenticated_login(self) -> str:
        return "mybot"


class _BuildFakeGH:
    """Minimal GitHub stand-in for _run_build label calls."""

    def ensure_label(self, *_args: object, **_kwargs: object) -> None: ...
    def add_labels(self, *_args: object, **_kwargs: object) -> None: ...
    def remove_label(self, *_args: object, **_kwargs: object) -> None: ...


def test_process_repo_caps_work_to_max_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    # Make _is_mock() true so _process_repo never tries to clone.
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")
    repo = RepoEntry(owner="octocat", name="hello")
    fake_gh = _FakeGitHub(5)

    # All five issues classify to "responder"; cap to 2.
    report = _process_repo(
        fake_gh,  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=True,
        log=lambda _m: None,
        max_issues=2,
    )
    assert len(report.actions) == 2
    assert report.issues_considered == 5  # uncapped count is recorded

    # Default (0) = unlimited: all five are processed.
    report = _process_repo(
        fake_gh,  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=True,
        log=lambda _m: None,
    )
    assert len(report.actions) == 5

    # Cap larger than the work list does not underflow.
    report = _process_repo(
        fake_gh,  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=True,
        log=lambda _m: None,
        max_issues=10,
    )
    assert len(report.actions) == 5


def test_process_repo_skips_locked_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")  # _is_mock(): skip the clone
    repo = RepoEntry(owner="octocat", name="hello")
    fake_gh = _FakeGitHub(1)

    # Hold the issue lock in-process; _process_repo's own RunLock (a separate fd)
    # then fails to acquire, so the issue is skipped without running an agent.
    with RunLock(ws.issue_lock_path(repo.owner, repo.name, 1)) as held:
        assert held.acquired
        report = _process_repo(
            fake_gh,  # ty: ignore[invalid-argument-type]
            ws,
            Config(),
            repo,
            "token",
            dry_run=False,
            log=lambda _m: None,
        )
    assert report.issues_considered == 1
    assert report.issues_skipped == 1
    assert report.turns_taken == 0


class _AdvancedUnderLockGH:
    """list_open_issues shows a responder issue, but get_issue shows it already advanced.

    Simulates an overlapping tick finalizing the issue between our classification and
    our acquiring its lock (the #57 race).
    """

    def __init__(self, ready_label: str) -> None:
        self._ready = ready_label

    def list_open_issues(self, _owner: str, _name: str) -> list[Issue]:
        return [Issue(number=1, title="t", body="b", state="open", labels=[], user="human", html_url="")]

    def list_issue_comments(self, _owner: str, _name: str, _number: int) -> list[Comment]:
        return []

    def get_issue(self, _owner: str, _name: str, _number: int) -> Issue:
        return Issue(number=1, title="t", body="b", state="open", labels=[self._ready], user="human", html_url="")

    def authenticated_login(self) -> str:
        return "mybot"


def test_process_repo_reclassifies_under_lock_and_skips_when_advanced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")  # _is_mock(): no clone
    repo = RepoEntry(owner="octocat", name="hello")
    gh = _AdvancedUnderLockGH(Config().labels["ready"])

    report = _process_repo(
        gh,  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=False,
        log=lambda _m: None,
    )
    # Classified as responder, but the under-lock re-check sees it is now `ready`
    # (stage "planner"), so it is skipped without running the responder.
    assert report.issues_considered == 1
    assert report.issues_skipped == 1
    assert report.turns_taken == 0
    assert any("already planner" in a for a in report.actions)


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


def test_process_repo_proceeds_when_stage_unchanged_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    decision = tmp_path / "decision.json"
    decision.write_text(
        '```json\n{"action": "comment", "reply": "one question?", "issue_md": ""}\n```',
        encoding="utf-8",
    )
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", str(decision))
    repo = RepoEntry(owner="octocat", name="hello")
    gh = _RecordingResponderGH()

    report = _process_repo(
        gh,  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=False,
        log=lambda _m: None,
    )
    # Stage is unchanged on re-check, so the responder runs and posts its comment.
    assert report.turns_taken == 1
    assert [n for n, _ in gh.comments] == [1]


def test_parse_depends_on() -> None:
    assert parse_depends_on("# Title\n\nDepends-on: #41, #42\n\n## Summary\n") == [41, 42]
    # Case-insensitive, "Depends on" spelling, bare numbers, dedup + sort.
    assert parse_depends_on("depends on: 7 and #3, #7") == [3, 7]
    # No directive -> empty.
    assert parse_depends_on("# Title\n\nNo dependencies here.\n") == []


class _IssuesGH:
    """GitHub stand-in returning a fixed issue list (for dependency-gating tests)."""

    def __init__(self, issues: list[Issue]) -> None:
        self._issues = issues

    def list_open_issues(self, _owner: str, _name: str) -> list[Issue]:
        return list(self._issues)

    def list_issue_comments(self, _owner: str, _name: str, _number: int) -> list[Comment]:
        return []

    def authenticated_login(self) -> str:
        return "mybot"


def test_process_repo_defers_issue_with_open_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")  # _is_mock(): no clone/agent
    repo = RepoEntry(owner="octocat", name="hello")
    lab = Config().labels

    # Issue #1 is ready to plan but its spec declares a dependency on open #2.
    issue_dir = ws.issue_dir(repo.owner, repo.name, 1)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Thing\n\nDepends-on: #2\n", encoding="utf-8")

    ready = Issue(number=1, title="t", body="b", state="open", labels=[lab["ready"]], user="u", html_url="")
    dep = Issue(number=2, title="t2", body="b", state="open", labels=[lab["needs_human"]], user="u", html_url="")

    # While #2 is open, #1 is deferred (and #2 itself is terminal, not actionable).
    report = _process_repo(
        _IssuesGH([ready, dep]),  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=False,
        log=lambda _m: None,
    )
    assert report.issues_considered == 0
    assert any("waiting on #2" in a for a in report.actions)

    # Once #2 is closed (absent from the open list), #1 is no longer deferred.
    report = _process_repo(
        _IssuesGH([ready]),  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "token",
        dry_run=True,  # dry-run: just confirm it is now considered work
        log=lambda _m: None,
    )
    assert report.issues_considered == 1
    assert not any("waiting" in a for a in report.actions)


def test_run_build_caps_tasks_per_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    issue = Issue(number=1, title="t", body="b", state="open", labels=[], user="u", html_url="")
    issue_dir = ws.issue_dir(repo.owner, repo.name, issue.number)
    store = TaskStore(issue_dir)
    _, tasks = planner.parse_manifest(
        {"tasks": [{"id": "t1", "title": "one"}, {"id": "t2", "title": "two"}, {"id": "t3", "title": "three"}]},
    )
    store.materialize(tasks)

    # Neutralise git + count worker invocations; each call marks its task done.
    monkeypatch.setattr(gitutil, "ensure_work_clone", lambda *_a, **_k: None)
    calls: list[str] = []

    def fake_run_task(_ws, _cfg, _repo, number, task, _work_dir, *, token):  # noqa: ANN001, ANN202
        calls.append(task.id)
        TaskStore(ws.issue_dir(repo.owner, repo.name, number)).set_status(task.id, DONE, summary="ok")
        return worker.WorkResult(task_id=task.id, status=DONE, committed=True)

    monkeypatch.setattr(worker, "run_task", fake_run_task)

    report = orchestrator.RepoReport(slug=repo.slug)
    cfg = Config.from_dict({"max_tasks_per_tick": 2})
    orchestrator._run_build(_BuildFakeGH(), ws, cfg, repo, issue, "token", lambda _m: None, report)

    # Only two of the three tasks ran this tick; the third remains pending.
    assert calls == ["t1", "t2"]
    assert not store.all_done()
    assert store.next_ready() is not None


def test_variant_for() -> None:
    cfg = Config()
    assert cfg.variant_for("responder") == "max"  # default cheap-but-strong tier
    assert cfg.variant_for("planner") == ""  # provider default
    # Round-trips through serialization.
    assert Config.from_dict(cfg.to_dict()).variant_for("responder") == "max"
    # Override.
    assert Config.from_dict({"variants": {"planner": "high"}}).variant_for("planner") == "high"


# --- env -------------------------------------------------------------------
def test_parse_env_handles_quotes_and_comments() -> None:
    parsed = parse_env('# comment\nexport A="hello"\nB=plain\n\nC=\n')
    assert parsed == {"A": "hello", "B": "plain", "C": ""}


def test_update_env_file_preserves_and_chmods(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    update_env_file(path, {"GITHUB_TOKEN": "abc"})
    update_env_file(path, {"OPENROUTER_API_KEY": "xyz"})
    values = read_env_file(path)
    assert values["GITHUB_TOKEN"] == "abc"
    assert values["OPENROUTER_API_KEY"] == "xyz"
    # Empty updates must not clobber existing secrets.
    update_env_file(path, {"GITHUB_TOKEN": ""})
    assert read_env_file(path)["GITHUB_TOKEN"] == "abc"
    assert (path.stat().st_mode & 0o077) == 0  # owner-only permissions


# --- registry --------------------------------------------------------------
def test_parse_slug_variants() -> None:
    assert parse_slug("octocat/Hello-World") == ("octocat", "Hello-World")
    assert parse_slug("https://github.com/octocat/Hello-World.git") == ("octocat", "Hello-World")
    with pytest.raises(ValueError, match="Invalid repository slug"):
        parse_slug("not-a-slug")


def test_registry_add_remove_toggle(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    assert reg.add(RepoEntry(owner="o", name="r")) is True
    assert reg.add(RepoEntry(owner="o", name="r")) is False  # duplicate
    assert reg.set_enabled("o", "r", enabled=False) is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.enabled is False
    assert reg.remove("o", "r") is True
    assert reg.remove("o", "r") is False


def test_registry_checks_roundtrip(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    assert RepoEntry(owner="o", name="r").checks == ""  # default: gate disabled
    assert RepoEntry(owner="o", name="r").autofix == ""  # default: auto-fix disabled
    reg.add(RepoEntry(owner="o", name="r", checks="ruff check . && pytest", autofix="ruff format ."))
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "ruff check . && pytest"
    assert found.autofix == "ruff format ."


def test_registry_update(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    reg.add(
        RepoEntry(
            owner="o",
            name="r",
            default_branch="develop",
            enabled=False,
            added_at="2024-01-01T00:00:00+00:00",
            checks="ruff check . && pytest",
            autofix="ruff format .",
        ),
    )

    # 1. Update only checks -> autofix is unchanged (and vice versa).
    assert reg.update("o", "r", checks="pytest -q") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "pytest -q"
    assert found.autofix == "ruff format ."

    assert reg.update("o", "r", autofix="ruff check --fix .") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "pytest -q"
    assert found.autofix == "ruff check --fix ."

    # 2. Update both checks and autofix in a single call.
    assert reg.update("o", "r", checks="make test", autofix="make format") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "make test"
    assert found.autofix == "make format"

    # 3. Setting checks="" (or autofix="") clears the value.
    assert reg.update("o", "r", checks="", autofix="") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == ""
    assert found.autofix == ""

    # 4. Updating a slug that isn't registered returns False and does not
    # create the file.
    missing_path = tmp_path / "missing.json"
    missing_reg = Registry(missing_path)
    assert missing_reg.update("unknown", "thing", checks="x") is False
    assert not missing_path.exists()

    # 5. update leaves enabled, default_branch, and added_at untouched when
    # called with only checks / autofix.
    assert reg.update("o", "r", checks="make lint", autofix="make fmt") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.enabled is False
    assert found.default_branch == "develop"
    assert found.added_at == "2024-01-01T00:00:00+00:00"

    # 6. Calling update with no kwargs (or only None kwargs) returns False
    # (no change), does not change any field, and does not rewrite the file.
    mtime_before = reg.path.stat().st_mtime_ns
    assert reg.update("o", "r") is False
    assert reg.update("o", "r", checks=None, autofix=None) is False
    mtime_after = reg.path.stat().st_mtime_ns
    assert mtime_before == mtime_after
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "make lint"
    assert found.autofix == "make fmt"


# --- responder -------------------------------------------------------------
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


def test_needs_turn_logic() -> None:
    cfg = Config()
    bot = "mybot"
    assert needs_turn(_issue(), [], cfg) is True  # fresh issue
    bot_c = _comment(BOT_MARKER + "\nQ?", bot)
    assert needs_turn(_issue(), [bot_c], cfg) is False  # waiting on human
    human_c = _comment("answer", "alice")
    assert needs_turn(_issue(), [bot_c, human_c], cfg) is True  # human replied
    ready = _issue(labels=[cfg.labels["ready"]])
    assert needs_turn(ready, [human_c], cfg) is False  # already finalized
    # Single-account scenario: a reply from the bot's own account but without the
    # marker must be treated as a human reply (not a bot comment).
    same_account_reply = _comment("got it", bot)
    assert needs_turn(_issue(), [bot_c, same_account_reply], cfg) is True


def test_is_bot_comment_single_account() -> None:
    """Verify is_bot_comment relies solely on the BOT_MARKER, not the author login.

    The bug: when the bot account is the same as the issue author, a plain human
    reply was misidentified as a bot comment because the old is_bot_comment did
    a ``comment.user == bot_login`` check as a fallback.

    Under the fix: only the presence of BOT_MARKER matters.
    """
    bot = "mybot"
    # Same user as bot, body has no marker → NOT a bot comment (the bug fix).
    assert is_bot_comment(_comment("plain reply", bot)) is False
    # Same user AND marker present → still a bot comment.
    assert is_bot_comment(_comment(f"{BOT_MARKER}\nhi", bot)) is True
    # Different user, marker present → still a bot comment (marker alone is enough).
    assert is_bot_comment(_comment(f"{BOT_MARKER}\nhi", "alice")) is True
    # Control: neither user nor marker matches.
    assert is_bot_comment(_comment("alice said hi", "alice")) is False


def test_build_prompt_includes_thread() -> None:
    prompt = build_prompt(_issue(number=42), [_comment("hi", "alice")])
    assert "Issue #42" in prompt
    assert "@alice" in prompt
    assert "hi" in prompt
    # Back-compat: omitted conventions kwarg does not emit a section.
    assert "## Repository conventions" not in prompt


def test_attribution_header_names_agent_and_model() -> None:
    header = attribution_header("responder", "openrouter/x")
    assert header.startswith(ATTRIBUTION_PREFIX)
    assert "responder" in header
    assert "openrouter/x" in header
    # A role with no backing model omits the model segment.
    assert "model" not in attribution_header("quality-gate", None)


def test_cheaphelp_message_prefixes_header_marker_and_is_detected() -> None:
    cfg = Config()  # responder has a model + the "max" variant by default
    body = cheaphelp_message("hello world", "responder", cfg)
    assert body.startswith(ATTRIBUTION_PREFIX)
    assert cfg.model_for("responder") in body
    assert "(max)" in body  # variant recorded
    assert BOT_MARKER in body
    assert "hello world" in body
    # The hidden marker keeps the comment recognisable as cheaphelp's own.
    assert is_bot_comment(_comment(body, "someone-else")) is True


def test_build_prompt_strips_attribution_header_from_thread() -> None:
    cfg = Config()
    own = cheaphelp_message("an earlier question", "responder", cfg)
    prompt = build_prompt(_issue(number=7), [_comment(own, "mybot")])
    # The visible header and hidden marker are not shown back to the responder.
    assert ATTRIBUTION_PREFIX not in prompt
    assert BOT_MARKER not in prompt
    assert "an earlier question" in prompt


# --- conventions injection into build_prompt --------------------------------


def test_responder_build_prompt_with_conventions() -> None:
    prompt = build_prompt(
        _issue(number=1),
        [_comment("hello", "alice")],
        conventions="house rules: no emoji",
    )
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_planner_build_prompt_with_conventions() -> None:
    prompt = planner.build_prompt("spec body", conventions="house rules: no emoji")
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_worker_build_prompt_with_conventions() -> None:
    prompt = worker.build_prompt(
        Task(id="t1", title="Do the thing"),
        "spec body",
        conventions="house rules: no emoji",
    )
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_reviewer_build_prompt_with_conventions() -> None:
    prompt = reviewer.build_prompt(
        "spec",
        "M file.py",
        "diff --git a/file.py b/file.py",
        "### t1: done\nok",
        conventions="house rules: no emoji",
    )
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_planner_build_prompt_no_conventions_by_default() -> None:
    prompt = planner.build_prompt("spec body")
    assert "## Repository conventions" not in prompt


def test_worker_build_prompt_no_conventions_by_default() -> None:
    prompt = worker.build_prompt(Task(id="t1", title="x"), "spec")
    assert "## Repository conventions" not in prompt


def test_reviewer_build_prompt_no_conventions_by_default() -> None:
    prompt = reviewer.build_prompt("spec", "M f.py", "diff", "summary")
    assert "## Repository conventions" not in prompt


def test_build_prompt_conventions_whitespace_only() -> None:
    prompt = build_prompt(_issue(), [], conventions="   ")
    assert "## Repository conventions" not in prompt


# --- opencode --------------------------------------------------------------
def test_extract_decision_from_messy_output() -> None:
    out = 'noise\n```json\n{"action": "comment", "reply": "hi"}\n```\n'
    assert opencode.extract_decision(out) == {"action": "comment", "reply": "hi"}


def test_extract_decision_prefers_last_block() -> None:
    out = '```json\n{"action": "comment"}\n```\n```json\n{"action": "finalize"}\n```'
    decision = opencode.extract_decision(out)
    assert decision is not None
    assert decision["action"] == "finalize"


def test_extract_decision_none_when_absent() -> None:
    assert opencode.extract_decision("just prose, no json") is None


def test_run_agent_reprompts_once_on_empty_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415 - local to keep the module import list lean

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        # First reply has no parseable json block; the retry returns a valid one.
        stdout = "thinking out loud, no json" if len(calls) == 1 else '```json\n{"status": "done"}\n```'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 2  # re-prompted exactly once
    assert opencode._REPROMPT_SUFFIX in calls[1][-1]  # the reminder rode along


def test_run_agent_no_reprompt_when_decision_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 1  # no retry needed


def test_run_agent_retries_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        if len(calls) <= 2:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 3


def test_run_agent_retries_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        if len(calls) <= 2:
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=1.0)
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 3


def test_run_agent_exhausted_retries_returns_last_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=2, stdout="", stderr="no good")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 3})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.returncode == 2
    assert result.ok is False
    assert len(calls) == 3


def test_run_agent_exhausted_timeout_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=1.0)

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 3})

    with pytest.raises(subprocess.TimeoutExpired):
        opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert len(calls) == 3


def test_run_agent_no_retry_on_clean_exit_with_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 1  # no retry, no re-prompt


def test_run_agent_reprompt_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        stdout = "thinking out loud, no json" if len(calls) == 1 else '```json\n{"status": "done"}\n```'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 2  # re-prompt, not retry loop


def test_run_agent_backoff_is_exponential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import random  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    random.seed(42)  # deterministic jitter
    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    monkeypatch.setattr(opencode.time, "sleep", fake_sleep)
    cfg = Config.from_dict({"retry_base_delay": 1.0, "retry_attempts": 3})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.returncode == 1
    assert len(calls) == 3
    assert len(sleeps) == 2

    # Attempt 1: base * 2^(0) = 1.0, jitter ±0.25
    assert 0.75 <= sleeps[0] <= 1.25
    # Attempt 2: base * 2^(1) = 2.0, jitter ±0.5
    assert 1.5 <= sleeps[1] <= 2.5


# --- UsageData --------------------------------------------------------------
def test_usage_data_defaults() -> None:
    u = opencode.UsageData()
    assert u.prompt_tokens == 0
    assert u.completion_tokens == 0
    assert u.total_tokens == 0
    assert u.cost_usd == 0.0


def test_usage_data_addition() -> None:
    a = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    b = opencode.UsageData(prompt_tokens=100, completion_tokens=200, total_tokens=300, cost_usd=0.01)
    c = a + b
    assert c.prompt_tokens == 110
    assert c.completion_tokens == 220
    assert c.total_tokens == 330
    assert c.cost_usd == 0.011
    # Original objects unchanged.
    assert a.prompt_tokens == 10
    assert b.prompt_tokens == 100


def test_usage_data_iadd() -> None:
    a = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    b = opencode.UsageData(prompt_tokens=100, completion_tokens=200, total_tokens=300, cost_usd=0.01)
    result = a.__iadd__(b)
    assert result is a  # returns self
    assert a.prompt_tokens == 110
    assert a.completion_tokens == 220
    assert a.total_tokens == 330
    assert a.cost_usd == 0.011


def test_usage_data_to_dict_roundtrip() -> None:
    u = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    d = u.to_dict()
    assert d == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost_usd": 0.001}
    restored = opencode.UsageData.from_dict(d)
    assert restored == u


def test_usage_data_from_dict_accepts_cost_key() -> None:
    # OpenRouter returns "cost" in the usage payload; accept it as cost_usd.
    u = opencode.UsageData.from_dict({"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.005})
    assert u.prompt_tokens == 10
    assert u.completion_tokens == 20
    assert u.cost_usd == 0.005
    # cost_usd key takes precedence over cost when both are present.
    u2 = opencode.UsageData.from_dict({"prompt_tokens": 1, "cost": 0.01, "cost_usd": 0.02})
    assert u2.cost_usd == 0.02


def test_usage_data_from_dict_coerces_types() -> None:
    u = opencode.UsageData.from_dict({"prompt_tokens": "10", "completion_tokens": "20", "cost": "0.005"})
    assert u.prompt_tokens == 10
    assert u.completion_tokens == 20
    assert u.cost_usd == 0.005


def test_usage_data_from_dict_empty() -> None:
    u = opencode.UsageData.from_dict({})
    assert u.prompt_tokens == 0
    assert u.completion_tokens == 0
    assert u.total_tokens == 0
    assert u.cost_usd == 0.0


# --- _parse_usage -----------------------------------------------------------
def test_parse_usage_from_stderr() -> None:
    usage = opencode._parse_usage("", '{"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.001}')
    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 20
    assert usage.cost_usd == 0.001


def test_parse_usage_from_stdout_when_stderr_empty() -> None:
    usage = opencode._parse_usage(
        '{"prompt_tokens": 5, "completion_tokens": 15, "cost_usd": 0.002}',
        "",
    )
    assert usage is not None
    assert usage.prompt_tokens == 5
    assert usage.completion_tokens == 15
    assert usage.cost_usd == 0.002


def test_parse_usage_nested_usage_key() -> None:
    payload = '{"id": "xyz", "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01}}'
    usage = opencode._parse_usage("", payload)
    assert usage is not None
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 50
    assert usage.cost_usd == 0.01


def test_parse_usage_line_by_line_fallback() -> None:
    # A mixed stderr with a JSON usage object on one line.
    stderr = 'some log info\n{"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.0005}\ndone'
    usage = opencode._parse_usage("", stderr)
    assert usage is not None
    assert usage.prompt_tokens == 7
    assert usage.completion_tokens == 3
    assert usage.cost_usd == 0.0005


def test_parse_usage_stderr_takes_precedence() -> None:
    usage = opencode._parse_usage(
        '{"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}',
        '{"prompt_tokens": 99, "completion_tokens": 99, "cost": 0.099}',
    )
    assert usage is not None
    assert usage.prompt_tokens == 99  # stderr won
    assert usage.completion_tokens == 99


def test_parse_usage_returns_none_when_no_json() -> None:
    assert opencode._parse_usage("just prose", "") is None
    assert opencode._parse_usage("", "more prose") is None
    assert opencode._parse_usage("", "") is None
    assert opencode._parse_usage('noise\n```json\n{"status": "ok"}\n```', "") is None


# --- run_agent usage population ---------------------------------------------
def test_run_agent_populates_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        stderr = '{"prompt_tokens": 50, "completion_tokens": 30, "cost": 0.004}'
        return SimpleNamespace(
            returncode=0,
            stdout='```json\n{"status": "done"}\n```',
            stderr=stderr,
        )

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 1
    assert result.usage is not None
    assert result.usage.prompt_tokens == 50
    assert result.usage.completion_tokens == 30
    assert result.usage.cost_usd == 0.004


# --- opencode config shape --------------------------------------------------
def test_build_opencode_config_shape() -> None:
    doc = opencode.build_opencode_config(Config())
    assert doc["$schema"] == opencode.OPENCODE_SCHEMA
    assert set(doc["agent"]) == {"responder", "planner", "worker", "reviewer", "rework"}
    # Responder is read-only; worker can write.
    assert doc["agent"]["responder"]["tools"]["edit"] is False
    assert doc["agent"]["worker"]["tools"]["edit"] is True
    # OpenRouter provider lists models without the opencode prefix.
    assert "deepseek/deepseek-v4-flash" in doc["provider"]["openrouter"]["models"]
    assert "minimax/minimax-m3" in doc["provider"]["openrouter"]["models"]


def test_sandbox_permissions_default_on() -> None:
    doc = opencode.build_opencode_config(Config())
    worker = doc["agent"]["worker"]["permission"]
    reader = doc["agent"]["planner"]["permission"]
    # Confined to the working directory, no network tools.
    assert worker["external_directory"] == "deny"
    assert reader["external_directory"] == "deny"
    assert reader["webfetch"] == "deny"
    # Worker: allow-by-default bash but dangerous commands denied; readers deny-default.
    assert worker["bash"]["*"] == "allow"
    assert worker["bash"]["sudo*"] == "deny"
    assert worker["bash"]["git push*"] == "deny"
    assert reader["bash"]["*"] == "deny"
    assert reader["bash"]["git status*"] == "allow"
    # Readers cannot edit; worker can.
    assert reader["edit"] == "deny"
    assert worker["edit"] == "allow"


def test_sandbox_can_be_disabled() -> None:
    cfg = Config.from_dict(
        {"sandbox": {"confine_to_workdir": False, "restrict_bash": False, "no_network_tools": False}},
    )
    perm = opencode.build_opencode_config(cfg)["agent"]["worker"]["permission"]
    assert perm["bash"] == "allow"
    assert "external_directory" not in perm
    assert "webfetch" not in perm


# --- systemd ---------------------------------------------------------------
def test_normalize_interval() -> None:
    assert systemd.normalize_interval("10m") == "10min"
    assert systemd.normalize_interval("2h") == "2h"
    assert systemd.normalize_interval("30s") == "30s"
    with pytest.raises(ValueError, match="Invalid interval"):
        systemd.normalize_interval("soon")


def test_render_units_contains_exec_and_interval(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m")
    assert "OnUnitActiveSec=15min" in units.timer
    assert "run --once" in units.service
    assert f"CHEAPHELP_HOME={tmp_path}" in units.service


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


# --- task store ------------------------------------------------------------
def test_task_store_lifecycle(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "issue-1")
    _, tasks = planner.parse_manifest(
        {
            "tasks": [
                {"id": "t1", "title": "one", "depends_on": []},
                {"id": "t2", "title": "two", "depends_on": ["t1"]},
            ],
        },
    )
    store.materialize(tasks)
    assert (tmp_path / "issue-1" / "tasks" / "t1.task.md").exists()

    # Only t1 is ready (t2 depends on it).
    ready = store.next_ready()
    assert ready is not None
    assert ready.id == "t1"
    assert not store.all_done()

    store.set_status("t1", DONE, summary="did one")
    ready = store.next_ready()
    assert ready is not None
    assert ready.id == "t2"
    store.set_status("t2", DONE)
    assert store.all_done()
    assert store.next_ready() is None


def test_task_store_blocked(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "issue-2")
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "x"}]})
    store.materialize(tasks)
    store.set_status("t1", "blocked")
    assert store.is_blocked()
    assert not store.all_done()


def test_task_store_record_attempt(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "issue-3")
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "x"}]})
    store.materialize(tasks)
    assert store.load()[0].attempts == 0
    assert store.record_attempt("t1") == 1
    assert store.record_attempt("t1") == 2
    assert store.load()[0].attempts == 2


# --- issue cost store -------------------------------------------------------
def test_issue_cost_store_load_missing_file_returns_zeros(tmp_path: Path) -> None:
    store = IssueCostStore(tmp_path / "issue-1")
    usage = store.load()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.cost_usd == 0.0


def test_issue_cost_store_add_returns_cumulative_total(tmp_path: Path) -> None:
    store = IssueCostStore(tmp_path / "issue-2")
    u1 = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    total = store.add(u1)
    assert total.prompt_tokens == 10
    assert total.completion_tokens == 20
    assert total.total_tokens == 30
    assert total.cost_usd == 0.001
    # Verify the file was written with the right shape.
    assert (tmp_path / "issue-2" / "cost.json").exists()
    data = json.loads((tmp_path / "issue-2" / "cost.json").read_text(encoding="utf-8"))
    assert data == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost_usd": 0.001}


def test_issue_cost_store_accumulates_across_instances(tmp_path: Path) -> None:
    """Two add() calls across separate IssueCostStore instances simulate restart."""
    store1 = IssueCostStore(tmp_path / "issue-3")
    store1.add(opencode.UsageData(prompt_tokens=5, completion_tokens=5, total_tokens=10, cost_usd=0.0005))

    store2 = IssueCostStore(tmp_path / "issue-3")
    total = store2.add(opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001))
    assert total.prompt_tokens == 15
    assert total.completion_tokens == 25
    assert total.total_tokens == 40
    assert total.cost_usd == 0.0015

    # Verify persistence: a third instance reads back the cumulative total.
    store3 = IssueCostStore(tmp_path / "issue-3")
    loaded = store3.load()
    assert loaded.prompt_tokens == 15
    assert loaded.completion_tokens == 25
    assert loaded.total_tokens == 40
    assert loaded.cost_usd == 0.0015


def test_issue_cost_store_corrupt_file_treated_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "issue-4" / "cost.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    store = IssueCostStore(tmp_path / "issue-4")
    usage = store.load()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.cost_usd == 0.0

    # Non-dict JSON is also treated as zero.
    path.write_text("[]", encoding="utf-8")
    usage = store.load()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.cost_usd == 0.0


def test_issue_cost_store_save_creates_parent_dir(tmp_path: Path) -> None:
    """save() creates the parent directory when it does not exist."""
    store = IssueCostStore(tmp_path / "a" / "b" / "issue-5")
    usage = opencode.UsageData(prompt_tokens=1, completion_tokens=2, total_tokens=3, cost_usd=0.0001)
    store.save(usage)
    assert store.path.exists()
    loaded = store.load()
    assert loaded.prompt_tokens == 1
    assert loaded.completion_tokens == 2
    assert loaded.total_tokens == 3
    assert loaded.cost_usd == 0.0001


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


def test_short_exc_collapses_timeout_and_truncates() -> None:
    import subprocess  # noqa: PLC0415 - local to keep the module import list lean

    # A subprocess timeout normally stringifies the whole command (incl. the
    # multi-KB prompt); _short_exc collapses it to a readable one-liner.
    timeout = subprocess.TimeoutExpired(cmd=["opencode", "run", "x" * 5000], timeout=600.0)
    assert _short_exc(timeout) == "agent timed out after 600s"
    # Long messages are truncated with an ellipsis; multiline is flattened.
    assert _short_exc(RuntimeError("a" * 500)).endswith("…")
    assert len(_short_exc(RuntimeError("a" * 500))) <= 201
    assert _short_exc(ValueError("line1\nline2")) == "line1 line2"
    # Short messages pass through unchanged.
    assert _short_exc(ValueError("boom")) == "boom"


# --- orchestrator stage classification ------------------------------------
def _issue_with(labels: list[str]) -> Issue:
    return Issue(number=1, title="t", body="b", state="open", labels=labels, user="u", html_url="")


def test_classify_stages() -> None:
    cfg = Config()
    lab = cfg.labels
    bot = "bot"
    # Fresh issue, human opened it -> responder.
    assert classify(_issue_with([]), [], cfg) == "responder"
    # Ready / needs-replan -> planner.
    assert classify(_issue_with([lab["ready"]]), [], cfg) == "planner"
    assert classify(_issue_with([lab["needs_replan"]]), [], cfg) == "planner"
    # Planned -> build.
    assert classify(_issue_with([lab["planned"]]), [], cfg) == "build"
    # In-progress alone (defensive) -> build.
    assert classify(_issue_with([lab["in_progress"]]), [], cfg) == "build"
    # Terminal / waiting -> named stage.
    assert classify(_issue_with([lab["rejected"]]), [], cfg) == "rejected"
    assert classify(_issue_with([lab["in_review"]]), [], cfg) == "rework"
    assert classify(_issue_with([lab["needs_human"]]), [], cfg) == "needs-human"
    # Idle: no label and the last comment is from the bot (no responder turn needed).
    bot_c = Comment(id=1, body=f"{BOT_MARKER}\nhi", user=bot, created_at="")
    assert classify(_issue_with([]), [bot_c], cfg) == "idle"
    # Terminal labels win over in-progress (precedence: rejected > in-review > needs-human).
    assert classify(_issue_with([lab["in_progress"], lab["in_review"]]), [], cfg) == "rework"


# --- workspace lock --------------------------------------------------------
def test_workspace_run_lock_path(tmp_path: Path) -> None:
    assert Workspace(tmp_path).run_lock_path == tmp_path / "run.lock"


def test_workspace_lock_paths(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    assert ws.locks_dir == tmp_path / "locks"
    assert ws.issue_lock_path("o", "r", 5) == tmp_path / "locks" / "o__r__issue-5.lock"
    assert ws.clone_lock_path("o", "r") == tmp_path / "locks" / "o__r__clone.lock"
    assert ws.locks_dir in ws.all_dirs


def test_run_lock_blocking_acquires_on_fresh_path(tmp_path: Path) -> None:
    with RunLock(tmp_path / "b.lock", blocking=True) as lock:
        assert lock.acquired is True


def test_run_lock_acquires_on_fresh_path(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with RunLock(path) as lock:
        assert lock.acquired is True
        assert path.exists()
        assert lock.holder_pid == os.getpid()


def test_run_lock_reports_contention(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with RunLock(path) as first:
        assert first.acquired is True
        with RunLock(path) as second:
            assert second.acquired is False


def test_run_lock_releases_on_exit(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with RunLock(path) as first:
        assert first.acquired is True
    with RunLock(path) as second:
        assert second.acquired is True


def test_run_lock_holder_pid(tmp_path: Path) -> None:
    with RunLock(tmp_path / "x.lock") as lock:
        assert lock.holder_pid == os.getpid()


# --- parse_diff_stat -------------------------------------------------------
def test_parse_diff_stat_plural_full() -> None:
    """Full stat line with multiple files, insertions and deletions."""
    result = gitutil.parse_diff_stat(
        " src/foo.py | 4 ++--\n src/bar.py | 2 +\n 2 files changed, 3 insertions(+), 3 deletions(-)",
    )
    assert result == (2, 3, 3)


def test_parse_diff_stat_singular_no_deletions() -> None:
    """Singular forms: 1 file, 1 insertion, no deletions."""
    result = gitutil.parse_diff_stat(" 1 file changed, 1 insertion(+)")
    assert result == (1, 1, 0)


def test_parse_diff_stat_singular_full() -> None:
    """Singular forms: 1 file, 1 insertion, 1 deletion."""
    result = gitutil.parse_diff_stat(" 1 file changed, 1 insertion(+), 1 deletion(-)")
    assert result == (1, 1, 1)


def test_parse_diff_stat_no_insertions() -> None:
    """Only deletions present (no insertions segment)."""
    result = gitutil.parse_diff_stat(" 3 files changed, 45 deletions(-)")
    assert result == (3, 0, 45)


def test_parse_diff_stat_empty() -> None:
    """Empty string returns None."""
    result = gitutil.parse_diff_stat("")
    assert result is None


def test_parse_diff_stat_garbage() -> None:
    """Unrecognisable prose returns None."""
    result = gitutil.parse_diff_stat("some prose, not a stat line")
    assert result is None


def test_parse_diff_stat_no_summary_line() -> None:
    """File-level diff lines with no summary line return None."""
    result = gitutil.parse_diff_stat(" src/foo.py | 4 ++--")
    assert result is None


# --- blast-radius guardrail ------------------------------------------------
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


def _blast_gh() -> _RecBuildGH:
    """Return a fresh _RecBuildGH and a default config/repo for blast-radius tests."""
    return _RecBuildGH()


def test_check_blast_radius_within_limits_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _blast_gh()
    repo = RepoEntry(owner="o", name="r")
    monkeypatch.setattr(gitutil, "diff_stat", lambda _w, _r: (5, 10, 5))
    report = orchestrator.RepoReport(slug=repo.slug)
    result = orchestrator._check_blast_radius(gh, None, Config(), repo, 1, None, lambda _m: None, report)
    assert result is True
    assert gh.calls == []


def test_check_blast_radius_files_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _blast_gh()
    repo = RepoEntry(owner="o", name="r")
    config = Config()
    monkeypatch.setattr(gitutil, "diff_stat", lambda _w, _r: (45, 10, 5))
    report = orchestrator.RepoReport(slug=repo.slug)
    result = orchestrator._check_blast_radius(gh, None, config, repo, 1, None, lambda _m: None, report)
    assert result is False

    # needs-human label was added.
    add_labels_call = next(c for c in gh.calls if c[0] == "add_labels")
    assert config.labels["needs_human"] in add_labels_call[1][-1]  # ty: ignore[unsupported-operator]

    # in-progress was removed.
    remove_label_call = next(c for c in gh.calls if c[0] == "remove_label")
    assert config.labels["in_progress"] in remove_label_call[1]

    # Comment body contains "45" and "blast-radius".
    comment_call = next(c for c in gh.calls if c[0] == "create_comment")
    body = comment_call[1][-1]
    assert "45" in body  # ty: ignore[unsupported-operator]
    assert "blast-radius" in body  # ty: ignore[unsupported-operator]


def test_check_blast_radius_lines_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _blast_gh()
    repo = RepoEntry(owner="o", name="r")
    config = Config()
    monkeypatch.setattr(gitutil, "diff_stat", lambda _w, _r: (5, 800, 800))
    report = orchestrator.RepoReport(slug=repo.slug)
    result = orchestrator._check_blast_radius(gh, None, config, repo, 1, None, lambda _m: None, report)
    assert result is False

    comment_call = next(c for c in gh.calls if c[0] == "create_comment")
    body = comment_call[1][-1]
    # The body lists individual insertions and deletions (not the sum).
    assert "Lines added: 800" in body  # ty: ignore[unsupported-operator]
    assert "Lines removed: 800" in body  # ty: ignore[unsupported-operator]
    add_labels_call = next(c for c in gh.calls if c[0] == "add_labels")
    assert config.labels["needs_human"] in add_labels_call[1][-1]  # ty: ignore[unsupported-operator]


def test_check_blast_radius_unlimited_when_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _blast_gh()
    repo = RepoEntry(owner="o", name="r", max_diff_files=0, max_diff_lines=0)
    monkeypatch.setattr(gitutil, "diff_stat", lambda _w, _r: (999, 9999, 9999))
    report = orchestrator.RepoReport(slug=repo.slug)
    result = orchestrator._check_blast_radius(gh, None, Config(), repo, 1, None, lambda _m: None, report)
    assert result is True
    assert gh.calls == []


def test_check_blast_radius_unparseable_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _blast_gh()
    repo = RepoEntry(owner="o", name="r")
    monkeypatch.setattr(gitutil, "diff_stat", lambda _w, _r: None)
    report = orchestrator.RepoReport(slug=repo.slug)
    result = orchestrator._check_blast_radius(gh, None, Config(), repo, 1, None, lambda _m: None, report)
    assert result is True
    assert gh.calls == []


def test_check_blast_radius_no_branch_push_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _blast_gh()
    repo = RepoEntry(owner="o", name="r")

    def raise_if_called(*_a: object, **_kw: object) -> None:
        raise AssertionError("push_branch should not be called")

    monkeypatch.setattr(gitutil, "push_branch", raise_if_called)
    monkeypatch.setattr(gitutil, "diff_stat", lambda _w, _r: (45, 10, 5))
    # This must not raise — proving push_branch was never invoked.
    report = orchestrator.RepoReport(slug=repo.slug)
    result = orchestrator._check_blast_radius(
        gh,
        None,
        Config(),
        repo,
        1,
        None,
        lambda _m: None,
        report,
    )
    assert result is False


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


# --- conventions ------------------------------------------------------------
def test_read_conventions_no_file(tmp_path: Path) -> None:
    assert read_conventions(tmp_path) == ""


def test_read_conventions_cheaphelp_md(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CHEAPHELP.md").write_text("hello", encoding="utf-8")
    assert read_conventions(d) == "hello"


def test_read_conventions_agents_md(tmp_path: Path) -> None:
    d = tmp_path
    (d / "AGENTS.md").write_text("world", encoding="utf-8")
    assert read_conventions(d) == "world"


def test_read_conventions_contributing_md(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CONTRIBUTING.md").write_text("contrib", encoding="utf-8")
    assert read_conventions(d) == "contrib"


def test_read_conventions_precedence_cheaphelp_wins(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CHEAPHELP.md").write_text("ch", encoding="utf-8")
    (d / "AGENTS.md").write_text("ag", encoding="utf-8")
    (d / "CONTRIBUTING.md").write_text("ct", encoding="utf-8")
    assert read_conventions(d) == "ch"


def test_read_conventions_precedence_agents_when_no_cheaphelp(tmp_path: Path) -> None:
    d = tmp_path
    (d / "AGENTS.md").write_text("ag", encoding="utf-8")
    (d / "CONTRIBUTING.md").write_text("ct", encoding="utf-8")
    assert read_conventions(d) == "ag"


def test_read_conventions_missing_dir(tmp_path: Path) -> None:
    assert read_conventions(tmp_path / "nope") == ""


def test_read_conventions_not_a_dir(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x", encoding="utf-8")
    assert read_conventions(f) == ""


def test_read_conventions_empty_file(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CHEAPHELP.md").write_text("", encoding="utf-8")
    assert read_conventions(d) == ""


def test_conventions_files_constant() -> None:
    assert CONVENTIONS_FILES == ("CHEAPHELP.md", "AGENTS.md", "CONTRIBUTING.md")


# --- conventions wiring integration tests -----------------------------------


def test_run_responder_forwards_conventions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_responder reads CHEAPHELP.md from clone dir and passes it to build_prompt."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / "CHEAPHELP.md").write_text("SENTINEL_HOUSE_RULES", encoding="utf-8")
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")

    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")

    captured: list[str] = []

    def recording_build_prompt(
        issue: object,
        comments: object,
        *,
        conventions: str = "",
    ) -> str:
        captured.append(conventions)
        return ""

    monkeypatch.setattr(responder, "build_prompt", recording_build_prompt)

    fake_gh = _RecordingResponderGH()
    report = orchestrator.RepoReport(slug=repo.slug)

    orchestrator._run_responder(
        fake_gh,
        ws,
        Config(),
        repo,
        fake_gh._issue(),
        [],
        clone_dir,
        lambda _m: None,
        report,
    )

    assert captured == ["SENTINEL_HOUSE_RULES"]


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


def test_run_responder_no_conventions_when_file_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_responder passes empty string when no conventions file exists."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")

    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    repo = RepoEntry(owner="octocat", name="hello")

    captured: list[str] = []

    def recording_build_prompt(
        issue: object,
        comments: object,
        *,
        conventions: str = "",
    ) -> str:
        captured.append(conventions)
        return ""

    monkeypatch.setattr(responder, "build_prompt", recording_build_prompt)

    fake_gh = _RecordingResponderGH()
    report = orchestrator.RepoReport(slug=repo.slug)

    orchestrator._run_responder(
        fake_gh,
        ws,
        Config(),
        repo,
        fake_gh._issue(),
        [],
        clone_dir,
        lambda _m: None,
        report,
    )

    assert captured == [""]


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


def test_git_run_error_redacts_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing git command must not leak the auth token in its GitError."""
    from types import SimpleNamespace  # noqa: PLC0415

    def fake_run(*_a: object, **_k: object) -> object:
        return SimpleNamespace(returncode=1, stdout="", stderr="remote rejected")

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    url = "https://x-access-token:supersecret_token@github.com/o/r.git"
    with pytest.raises(gitutil.GitError) as excinfo:
        gitutil._run(["push", url, "HEAD:refs/heads/b"])
    message = str(excinfo.value)
    assert "supersecret_token" not in message
    assert "***@github.com" in message


# --- cost recording ----------------------------------------------------------


def test_run_responder_records_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_responder records token/cost data via _record_cost."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")  # skip clone

    repo = RepoEntry(owner="octocat", name="hello")
    issue = Issue(number=1, title="t", body="b", state="open", labels=[], user="alice", html_url="")

    usage = opencode.UsageData(prompt_tokens=100, completion_tokens=50, cost_usd=0.002)
    result = opencode.AgentResult(
        returncode=0,
        stdout='```json\n{"action":"comment","reply":"q?"}\n```',
        stderr="",
        decision={"action": "comment", "reply": "q?"},
        usage=usage,
    )
    monkeypatch.setattr(opencode, "run_agent", lambda *a, **kw: result)

    fake_gh = _RecordingResponderGH()
    report = orchestrator.RepoReport(slug=repo.slug)

    orchestrator._run_responder(
        fake_gh,
        ws,
        Config(),
        repo,
        issue,
        [],
        tmp_path,
        lambda _m: None,
        report,
    )

    assert report.cost == usage
    assert report.issue_costs[1]["responder"] == [usage]

    # Verify cost.json was written and round-trips.
    cost_path = ws.issue_dir(repo.owner, repo.name, 1) / "cost.json"
    assert cost_path.exists()
    loaded = IssueCostStore(ws.issue_dir(repo.owner, repo.name, 1)).load()
    assert loaded == usage


def test_run_responder_skips_cost_when_usage_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result with usage=None is a no-op for cost recording."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")

    repo = RepoEntry(owner="octocat", name="hello")
    issue = Issue(number=1, title="t", body="b", state="open", labels=[], user="alice", html_url="")

    result = opencode.AgentResult(
        returncode=0,
        stdout='```json\n{"action":"comment","reply":"q?"}\n```',
        stderr="",
        decision={"action": "comment", "reply": "q?"},
        usage=None,
    )
    monkeypatch.setattr(opencode, "run_agent", lambda *a, **kw: result)

    fake_gh = _RecordingResponderGH()
    report = orchestrator.RepoReport(slug=repo.slug)

    orchestrator._run_responder(
        fake_gh,
        ws,
        Config(),
        repo,
        issue,
        [],
        tmp_path,
        lambda _m: None,
        report,
    )

    assert report.cost == opencode.UsageData()
    assert report.issue_costs == {}
    cost_path = ws.issue_dir(repo.owner, repo.name, 1) / "cost.json"
    assert not cost_path.exists()


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


def test_tick_report_total_cost_sums_across_repos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TickReport.total_cost equals the sum of RepoReport.cost across repos."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    # Register two repos.
    reg = Registry(ws.registry_path)
    reg.add(RepoEntry(owner="octocat", name="hello", enabled=True))
    reg.add(RepoEntry(owner="octocat", name="world", enabled=True))

    # Mock the GitHub client so the tick never touches the network: both repos
    # report zero open issues, so each yields a RepoReport with cost=UsageData().
    class _NoIssuesGH:
        def __enter__(self) -> _NoIssuesGH:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def authenticated_login(self) -> str:
            return "mybot"

        def list_open_issues(self, _owner: str, _name: str) -> list[Issue]:
            return []

    monkeypatch.setattr(orchestrator, "GitHubClient", lambda *_a, **_k: _NoIssuesGH())

    report = orchestrator.tick(ws, log=lambda _m: None)

    # Both repos have cost=UsageData() (no agent ran, mock mode /dev/null).
    assert len(report.repos) == 2
    assert report.total_cost == opencode.UsageData()
    assert report.total_cost == report.repos[0].cost + report.repos[1].cost


# --- PR review API ---------------------------------------------------------


def _make_client() -> GitHubClient:
    """Build a GitHubClient with a non-empty token for testing."""
    return GitHubClient("test-token")


def test_get_pull_request_returns_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    canned = {"number": 7, "state": "open", "head": {"sha": "abc123"}, "requested_reviewers": [{"login": "alice"}]}

    def fake_request(method: str, path: str, **kwargs: object) -> dict:
        assert path == "/repos/owner/repo/pulls/7"
        return canned

    monkeypatch.setattr(gh, "_request", fake_request)
    result = gh.get_pull_request("owner", "repo", 7)
    assert result["number"] == 7
    assert result["head"]["sha"] == "abc123"


def test_list_pr_reviews_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    canned = [
        {"id": 1, "state": "CHANGES_REQUESTED", "user": {"login": "alice"}, "submitted_at": "2024-01-01T00:00:00Z"},
        {"id": 2, "state": "APPROVED", "user": {"login": "bob"}, "submitted_at": "2024-01-02T00:00:00Z"},
    ]

    def fake_paginate(path: str, **params: object) -> list[dict]:
        assert path == "/repos/owner/repo/pulls/7/reviews"
        return canned

    monkeypatch.setattr(gh, "_paginate", fake_paginate)
    result = gh.list_pr_reviews("owner", "repo", 7)
    assert len(result) == 2
    assert result[0]["state"] == "CHANGES_REQUESTED"
    assert result[1]["user"]["login"] == "bob"


def test_list_pr_reviews_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    monkeypatch.setattr(gh, "_paginate", lambda _path, **_: [])
    assert gh.list_pr_reviews("owner", "repo", 7) == []


def test_list_pr_review_comments_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    canned = [
        {
            "id": 42,
            "body": "please fix this",
            "user": {"login": "reviewer1"},
            "created_at": "2024-01-01T00:00:00Z",
            "path": "src/main.py",
            "line": 15,
            "commit_id": "abc123",
        },
    ]

    def fake_paginate(path: str, **params: object) -> list[dict]:
        assert path == "/repos/owner/repo/pulls/7/comments"
        return canned

    monkeypatch.setattr(gh, "_paginate", fake_paginate)
    result = gh.list_pr_review_comments("owner", "repo", 7)
    assert len(result) == 1
    assert isinstance(result[0], PRReviewComment)
    assert result[0].id == 42
    assert result[0].body == "please fix this"
    assert result[0].user == "reviewer1"
    assert result[0].path == "src/main.py"
    assert result[0].line == 15
    assert result[0].commit_id == "abc123"


def test_pr_review_comment_from_payload_tolerates_missing_path_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review comment at the file level (not line-level) may omit path/line."""
    gh = _make_client()
    canned = [
        {
            "id": 99,
            "body": "overall file feedback",
            "user": {"login": "reviewer2"},
            "created_at": "2024-01-01T00:00:00Z",
            "commit_id": "def456",
            # No "path" or "line" key.
        },
    ]

    monkeypatch.setattr(gh, "_paginate", lambda _path, **_: canned)
    result = gh.list_pr_review_comments("owner", "repo", 7)
    assert len(result) == 1
    assert result[0].id == 99
    assert result[0].path == ""  # default empty string
    assert result[0].line is None  # explicit None


def test_pr_review_comment_from_payload_line_none() -> None:
    """from_payload handles line=null correctly."""
    data: dict[str, object] = {
        "id": 1,
        "body": "comment",
        "user": {"login": "u"},
        "created_at": "",
        "path": "file.py",
        "line": None,
        "commit_id": "c1",
    }
    comment = PRReviewComment.from_payload(data)
    assert comment.line is None
    assert comment.path == "file.py"


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


def test_rev_parse_returns_sha(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    gitutil._run(["init"], cwd=repo)
    gitutil._run(["config", "user.email", "test@test"], cwd=repo)
    gitutil._run(["config", "user.name", "Test"], cwd=repo)
    gitutil._run(["commit", "--allow-empty", "-m", "first"], cwd=repo)
    sha = gitutil.rev_parse(repo)
    assert isinstance(sha, str)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


# --- rework ------------------------------------------------------------------

from cheaphelp._internal.rework import ReworkResult, run_rework  # noqa: E402


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
