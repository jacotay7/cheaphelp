"""Tests for cheaphelp's `retry` subcommand."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cheaphelp import main
from cheaphelp._internal import commands
from cheaphelp._internal.config import Workspace
from cheaphelp._internal.github import GitHubError, Issue
from cheaphelp._internal.registry import Registry, RepoEntry
from cheaphelp._internal.tasks import TaskStore
from tests.conftest import _TEST_TOKEN, _FakeGH, _seed_workspace_env, _setup_workspace, _usage_data


# --- retry -------------------------------------------------------------------
class _RetryFakeGH(_FakeGH):
    """Extends _FakeGH with methods used by cmd_retry and call recording."""

    def __init__(self, token: str, **kwargs: object) -> None:
        super().__init__(token, **kwargs)
        self.calls: list[tuple[str, tuple, dict]] = []
        self.issue: Issue | None = None

    def _record(
        self,
        method: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        self.calls.append((method, args, kwargs))

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        self._record("get_issue", owner, repo, number)
        if self.issue is None:
            msg = "404 - Not Found"
            raise GitHubError(msg)
        return self.issue

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self._record("remove_label", owner, repo, number, label)

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        self._record("add_labels", owner, repo, number, labels)

    def ensure_label(
        self,
        owner: str,
        repo: str,
        name: str,
        *,
        color: str = "ededed",
        description: str = "",
    ) -> None:
        self._record("ensure_label", owner, repo, name, color=color, description=description)


def _pre_seed_issue_dir(
    ws: Workspace,
    owner: str,
    name: str,
    number: int,
    *,
    tasks_payload: dict | None = None,
    issues_md: str | None = None,
    replan_md: str | None = None,
) -> Path:
    """Seed an issue directory with tasks.json, issues.md, and/or replan.md."""
    issue_dir = ws.issue_dir(owner, name, number)
    issue_dir.mkdir(parents=True, exist_ok=True)
    if tasks_payload is not None:
        (issue_dir / "tasks.json").write_text(
            json.dumps(tasks_payload, indent=2) + "\n",
            encoding="utf-8",
        )
    if issues_md is not None:
        (issue_dir / "issues.md").write_text(issues_md, encoding="utf-8")
    if replan_md is not None:
        (issue_dir / "replan.md").write_text(replan_md, encoding="utf-8")
    return issue_dir


def _retry_fake_tick(
    workspace: Workspace,
    *,
    dry_run: bool,
    log: Callable[[str], None],
    max_issues: int = 0,
) -> SimpleNamespace:
    log("hello-from-retry")
    return SimpleNamespace(error=None, total_turns=1, repos=[], total_cost=_usage_data())


def test_retry_happy_path_removes_labels_and_resets_tasks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: removes labels, adds needs-replan, resets tasks, deletes replan.md, runs tick."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    config = ws.load_config()
    lab = config.labels

    # Pre-seed tasks.json with one PENDING task (attempts=1) and one BLOCKED task.
    tasks_payload = {
        "tasks": [
            {
                "id": "t1",
                "title": "Task one",
                "status": "pending",
                "attempts": 1,
                "summary": "",
                "depends_on": [],
                "files": [],
                "details": "",
                "verify": "",
            },
            {
                "id": "t2",
                "title": "Task two",
                "status": "blocked",
                "attempts": 2,
                "summary": "",
                "depends_on": [],
                "files": [],
                "details": "",
                "verify": "",
            },
        ],
    }
    issue_md_text = "# Issue spec\n\nSome spec text.\n"
    _pre_seed_issue_dir(
        ws,
        "octocat",
        "hello",
        42,
        tasks_payload=tasks_payload,
        issues_md=issue_md_text,
        replan_md="# Stale replan",
    )

    monkeypatch.setattr(commands, "tick", _retry_fake_tick)

    fake = _RetryFakeGH("test-token")
    fake.issue = Issue(
        number=42,
        title="Stuck issue",
        body="",
        state="open",
        labels=[lab["needs_human"], lab["in_progress"]],
        user="alice",
        html_url="",
    )

    def _factory(token: str, **_kwargs: object) -> _RetryFakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "retry", "octocat/hello", "42", "--yes"])
    assert rc == 0, f"rc={rc}, stderr={capsys.readouterr().err}"

    captured = capsys.readouterr()
    assert "hello-from-retry" in captured.out

    # Tick was called.
    # Verify label operations.
    calls = fake.calls
    remove_calls = [c for c in calls if c[0] == "remove_label"]
    assert len(remove_calls) == 2
    remove_args = [c[1][3] for c in remove_calls]  # the label argument
    assert lab["needs_human"] in remove_args
    assert lab["in_progress"] in remove_args

    ensure_calls = [c for c in calls if c[0] == "ensure_label"]
    assert len(ensure_calls) == 1
    assert ensure_calls[0][1][2] == lab["needs_replan"]

    add_calls = [c for c in calls if c[0] == "add_labels"]
    assert len(add_calls) == 1
    assert add_calls[0][1][3] == [lab["needs_replan"]]

    # Task state reset.
    store = TaskStore(ws.issue_dir("octocat", "hello", 42))
    tasks = store.load()
    assert len(tasks) == 2
    by_id = {t.id: t for t in tasks}
    assert by_id["t1"].attempts == 0
    assert by_id["t1"].status == "pending"
    assert by_id["t2"].attempts == 0
    assert by_id["t2"].status == "pending"  # was BLOCKED

    # replan.md is gone.
    assert not (ws.issue_dir("octocat", "hello", 42) / "replan.md").exists()

    # issues.md is unchanged.
    assert (ws.issue_dir("octocat", "hello", 42) / "issues.md").read_text(encoding="utf-8") == issue_md_text


def test_retry_dry_run_makes_no_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run prints actions but makes no API calls or filesystem changes."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    config = ws.load_config()
    lab = config.labels

    tasks_payload = {
        "tasks": [
            {
                "id": "t1",
                "title": "Task one",
                "status": "pending",
                "attempts": 1,
                "summary": "",
                "depends_on": [],
                "files": [],
                "details": "",
                "verify": "",
            },
            {
                "id": "t2",
                "title": "Task two",
                "status": "blocked",
                "attempts": 2,
                "summary": "",
                "depends_on": [],
                "files": [],
                "details": "",
                "verify": "",
            },
        ],
    }
    _pre_seed_issue_dir(
        ws,
        "octocat",
        "hello",
        42,
        tasks_payload=tasks_payload,
        replan_md="# Stale replan",
    )

    tasks_path = ws.issue_dir("octocat", "hello", 42) / "tasks.json"
    before_bytes = tasks_path.read_bytes()

    monkeypatch.setattr(commands, "tick", _retry_fake_tick)

    fake = _RetryFakeGH("test-token")
    fake.issue = Issue(
        number=42,
        title="Stuck issue",
        body="",
        state="open",
        labels=[lab["needs_human"], lab["in_progress"]],
        user="alice",
        html_url="",
    )

    def _factory(token: str, **_kwargs: object) -> _RetryFakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "retry", "octocat/hello", "42", "--dry-run", "--yes"])
    assert rc == 0, f"rc={rc}, stderr={capsys.readouterr().err}"

    captured = capsys.readouterr()
    assert "Dry run" in captured.out

    # tick was NOT called (no "hello-from-retry" in output).
    assert "hello-from-retry" not in captured.out

    # No mutation API calls were made (get_issue is still called to verify the issue).
    mutation_methods = {"remove_label", "add_labels", "ensure_label"}
    assert not any(c[0] in mutation_methods for c in fake.calls)

    # tasks.json is byte-identical.
    after_bytes = tasks_path.read_bytes()
    assert before_bytes == after_bytes

    # replan.md still exists.
    assert (ws.issue_dir("octocat", "hello", 42) / "replan.md").exists()


def test_retry_yes_skips_prompt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --yes, the confirmation prompt is skipped (input is never called)."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    config = ws.load_config()
    lab = config.labels

    monkeypatch.setattr(
        "builtins.input",
        lambda _="": (_ for _ in ()).throw(AssertionError("input should not be called")),
    )
    monkeypatch.setattr(commands, "tick", _retry_fake_tick)

    fake = _RetryFakeGH("test-token")
    fake.issue = Issue(
        number=42,
        title="Stuck issue",
        body="",
        state="open",
        labels=[lab["needs_human"]],
        user="alice",
        html_url="",
    )

    def _factory(token: str, **_kwargs: object) -> _RetryFakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "retry", "octocat/hello", "42", "--yes"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "hello-from-retry" in captured.out


def test_retry_no_prompt_in_non_tty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --yes in a non-TTY, the command refuses."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    config = ws.load_config()
    lab = config.labels

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    fake = _RetryFakeGH("test-token")
    fake.issue = Issue(
        number=42,
        title="Stuck issue",
        body="",
        state="open",
        labels=[lab["needs_human"]],
        user="alice",
        html_url="",
    )

    def _factory(token: str, **_kwargs: object) -> _RetryFakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    # Capture the tasks.json before.
    _pre_seed_issue_dir(
        ws,
        "octocat",
        "hello",
        42,
        tasks_payload={
            "tasks": [
                {
                    "id": "t1",
                    "title": "T",
                    "status": "pending",
                    "attempts": 1,
                    "summary": "",
                    "depends_on": [],
                    "files": [],
                    "details": "",
                    "verify": "",
                },
            ],
        },
    )
    tasks_path = ws.issue_dir("octocat", "hello", 42) / "tasks.json"
    before_bytes = tasks_path.read_bytes()

    monkeypatch.setattr(commands, "tick", _retry_fake_tick)

    rc = main(["--home", str(ws.home), "retry", "octocat/hello", "42"])
    assert rc == 1, f"rc={rc}"

    captured = capsys.readouterr()
    assert "non-interactive" in captured.err.lower()

    # No tick call (no "hello-from-retry" in stdout).
    assert "hello-from-retry" not in captured.out

    # tasks.json unchanged.
    assert tasks_path.read_bytes() == before_bytes


def test_retry_unknown_repo_exits_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry on an unregistered repo exits 1 with an error message."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    # No repo added.

    def _exploding_factory(_token: str, **_kwargs: object) -> _RetryFakeGH:
        msg = "GitHubClient should not be instantiated when the repo is not registered"
        raise AssertionError(msg)

    monkeypatch.setattr(commands, "GitHubClient", _exploding_factory)

    rc = main(["--home", str(ws.home), "retry", "unknown/repo", "42", "--yes"])
    assert rc == 1

    err = capsys.readouterr().err
    assert "unknown/repo is not registered." in err


def test_retry_issue_without_needs_human_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry on an issue without 'needs-human' exits 2 with an error."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    # Pre-seed a tasks.json to assert it's not touched.
    tasks_payload = {
        "tasks": [
            {
                "id": "t1",
                "title": "T",
                "status": "pending",
                "attempts": 1,
                "summary": "",
                "depends_on": [],
                "files": [],
                "details": "",
                "verify": "",
            },
        ],
    }
    _pre_seed_issue_dir(ws, "octocat", "hello", 42, tasks_payload=tasks_payload)
    tasks_path = ws.issue_dir("octocat", "hello", 42) / "tasks.json"
    before_bytes = tasks_path.read_bytes()

    fake = _RetryFakeGH("test-token")
    # Issue has a different label, NOT needs_human.
    fake.issue = Issue(
        number=42,
        title="Some issue",
        body="",
        state="open",
        labels=["some_other_label"],
        user="alice",
        html_url="",
    )

    def _factory(token: str, **_kwargs: object) -> _RetryFakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)
    monkeypatch.setattr(commands, "tick", _retry_fake_tick)

    rc = main(["--home", str(ws.home), "retry", "octocat/hello", "42", "--yes"])
    assert rc == 2, f"rc={rc}"

    captured = capsys.readouterr()
    assert "not labeled" in captured.err

    # No mutation calls (remove_label, add_labels, ensure_label) were made.
    mutation_methods = {"remove_label", "add_labels", "ensure_label"}
    assert not any(c[0] in mutation_methods for c in fake.calls)

    # No tick call.
    assert "hello-from-retry" not in captured.out

    # tasks.json unchanged.
    assert tasks_path.read_bytes() == before_bytes


def test_retry_issue_not_found_exits_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry on a non-existent issue exits 1 with the GitHub API error."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    fake = _RetryFakeGH("test-token")
    # No issue set -> get_issue will raise GitHubError("404 - Not Found")

    def _factory(token: str, **_kwargs: object) -> _RetryFakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)
    monkeypatch.setattr(commands, "tick", _retry_fake_tick)

    rc = main(["--home", str(ws.home), "retry", "octocat/hello", "42", "--yes"])
    assert rc == 1, f"rc={rc}"

    err = capsys.readouterr().err
    assert "404" in err or "GitHub API error" in err


def test_retry_invalid_slug_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid slug exits 2 without constructing a GitHub client."""
    ws = _setup_workspace(tmp_path)

    def _exploding_factory(_token: str, **_kwargs: object) -> _RetryFakeGH:
        msg = "GitHubClient should not be instantiated on invalid slug"
        raise AssertionError(msg)

    monkeypatch.setattr(commands, "GitHubClient", _exploding_factory)

    rc = main(["--home", str(ws.home), "retry", "not a slug", "42", "--yes"])
    assert rc == 2, f"rc={rc}"

    err = capsys.readouterr().err
    assert "Invalid repository slug" in err


def test_retry_preserves_done_tasks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry preserves DONE tasks and their summaries; only resets BLOCKED->PENDING and zeroes attempts."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    config = ws.load_config()
    lab = config.labels

    # Pre-seed tasks.json with one DONE task (with summary) and one BLOCKED task.
    tasks_payload = {
        "tasks": [
            {
                "id": "t1",
                "title": "Done task",
                "status": "done",
                "attempts": 1,
                "summary": "This task was completed successfully.",
                "depends_on": [],
                "files": [],
                "details": "",
                "verify": "",
            },
            {
                "id": "t2",
                "title": "Blocked task",
                "status": "blocked",
                "attempts": 3,
                "summary": "",
                "depends_on": [],
                "files": [],
                "details": "",
                "verify": "",
            },
        ],
    }
    issue_dir = _pre_seed_issue_dir(ws, "octocat", "hello", 42, tasks_payload=tasks_payload)

    monkeypatch.setattr(commands, "tick", _retry_fake_tick)

    fake = _RetryFakeGH("test-token")
    fake.issue = Issue(
        number=42,
        title="Stuck issue",
        body="",
        state="open",
        labels=[lab["needs_human"]],
        user="alice",
        html_url="",
    )

    def _factory(token: str, **_kwargs: object) -> _RetryFakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "retry", "octocat/hello", "42", "--yes"])
    assert rc == 0, f"rc={rc}, stderr={capsys.readouterr().err}"

    capsys.readouterr()  # discard output

    # Reload TaskStore.
    store = TaskStore(issue_dir)
    tasks = store.load()
    by_id = {t.id: t for t in tasks}

    # DONE task preserved.
    assert by_id["t1"].status == "done"
    assert by_id["t1"].summary == "This task was completed successfully."
    # Attempts reset to 0 (reset_all resets all attempts).
    assert by_id["t1"].attempts == 0

    # BLOCKED task is now PENDING with attempts=0.
    assert by_id["t2"].status == "pending"
    assert by_id["t2"].attempts == 0
