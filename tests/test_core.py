"""Tests for cheaphelp's core logic that does not require network access."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cheaphelp._internal import cleanup, gitutil, opencode, orchestrator, planner, responder, reviewer, systemd, worker
from cheaphelp._internal.config import DEFAULT_AGENT_TIMEOUT, DEFAULT_MODELS, Config, Workspace
from cheaphelp._internal.conventions import CONVENTIONS_FILES, read_conventions
from cheaphelp._internal.env import parse_env, read_env_file, update_env_file
from cheaphelp._internal.github import Comment, Issue
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
from cheaphelp._internal.tasks import BLOCKED, DONE, PENDING, Task, TaskStore


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
        "mybot",
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
        "mybot",
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
        "mybot",
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
            "mybot",
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
        "mybot",
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
        "mybot",
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
    dep = Issue(number=2, title="t2", body="b", state="open", labels=[lab["in_review"]], user="u", html_url="")

    # While #2 is open, #1 is deferred (and #2 itself is terminal, not actionable).
    report = _process_repo(
        _IssuesGH([ready, dep]),  # ty: ignore[invalid-argument-type]
        ws,
        Config(),
        repo,
        "mybot",
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
        "mybot",
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
    assert needs_turn(_issue(), [], bot, cfg) is True  # fresh issue
    bot_c = _comment(BOT_MARKER + "\nQ?", bot)
    assert needs_turn(_issue(), [bot_c], bot, cfg) is False  # waiting on human
    human_c = _comment("answer", "alice")
    assert needs_turn(_issue(), [bot_c, human_c], bot, cfg) is True  # human replied
    ready = _issue(labels=[cfg.labels["ready"]])
    assert needs_turn(ready, [human_c], bot, cfg) is False  # already finalized


def test_build_prompt_includes_thread() -> None:
    prompt = build_prompt(_issue(number=42), [_comment("hi", "alice")], "mybot")
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
    assert is_bot_comment(_comment(body, "someone-else"), "mybot") is True


def test_build_prompt_strips_attribution_header_from_thread() -> None:
    cfg = Config()
    own = cheaphelp_message("an earlier question", "responder", cfg)
    prompt = build_prompt(_issue(number=7), [_comment(own, "mybot")], "mybot")
    # The visible header and hidden marker are not shown back to the responder.
    assert ATTRIBUTION_PREFIX not in prompt
    assert BOT_MARKER not in prompt
    assert "an earlier question" in prompt


# --- conventions injection into build_prompt --------------------------------


def test_responder_build_prompt_with_conventions() -> None:
    prompt = build_prompt(
        _issue(number=1),
        [_comment("hello", "alice")],
        "mybot",
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
    prompt = build_prompt(_issue(), [], "bot", conventions="   ")
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


def test_build_opencode_config_shape() -> None:
    doc = opencode.build_opencode_config(Config())
    assert doc["$schema"] == opencode.OPENCODE_SCHEMA
    assert set(doc["agent"]) == {"responder", "planner", "worker", "reviewer"}
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
    assert classify(_issue_with([]), [], bot, cfg) == "responder"
    # Ready / needs-replan -> planner.
    assert classify(_issue_with([lab["ready"]]), [], bot, cfg) == "planner"
    assert classify(_issue_with([lab["needs_replan"]]), [], bot, cfg) == "planner"
    # Planned -> build.
    assert classify(_issue_with([lab["planned"]]), [], bot, cfg) == "build"
    # In-progress alone (defensive) -> build.
    assert classify(_issue_with([lab["in_progress"]]), [], bot, cfg) == "build"
    # Terminal / waiting -> named stage.
    assert classify(_issue_with([lab["rejected"]]), [], bot, cfg) == "rejected"
    assert classify(_issue_with([lab["in_review"]]), [], bot, cfg) == "in-review"
    assert classify(_issue_with([lab["needs_human"]]), [], bot, cfg) == "needs-human"
    # Idle: no label and the last comment is from the bot (no responder turn needed).
    bot_c = Comment(id=1, body="hi", user=bot, created_at="")
    assert classify(_issue_with([]), [bot_c], bot, cfg) == "idle"
    # Terminal labels win over in-progress (precedence: rejected > in-review > needs-human).
    assert classify(_issue_with([lab["in_progress"], lab["in_review"]]), [], bot, cfg) == "in-review"


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
    assert config.labels["in_progress"] in remove_label_call[1]  # ty: ignore[unsupported-operator]

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
    needs_human_added = any(config.labels["needs_human"] in c[1][-1] for c in add_labels_calls)
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
        bot_login: object,
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
        "mybot",
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
        bot_login: object,
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
        "mybot",
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
        lambda *a, **kw: SimpleNamespace(decision={"status": "done", "summary": "ok"}),
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
        lambda *a, **kw: SimpleNamespace(decision={"decision": "open_pr", "pr_title": "x", "pr_body": "y"}),
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
