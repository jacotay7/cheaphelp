"""Tests for cheaphelp's orchestrator: classification, dispatch, and tick reporting."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cheaphelp._internal import (
    gitutil,
    opencode,
    orchestrator,
    planner,
    responder,
    worker,
)
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.lock import RunLock
from cheaphelp._internal.orchestrator import _process_repo, _short_exc, classify, parse_depends_on
from cheaphelp._internal.registry import Registry, RepoEntry
from cheaphelp._internal.responder import (
    BOT_MARKER,
)
from cheaphelp._internal.tasks import DONE, IssueCostStore, TaskStore


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

    def ensure_label(self, *_args: object, **_kwargs: object) -> None: ...
    def add_labels(self, *_args: object, **_kwargs: object) -> None: ...
    def remove_label(self, *_args: object, **_kwargs: object) -> None: ...


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
