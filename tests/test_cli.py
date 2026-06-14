"""Tests for the CLI."""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cheaphelp import main
from cheaphelp._internal import commands, debug, orchestrator
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.env import GITHUB_TOKEN_KEY, update_env_file
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.lock import RunLock
from cheaphelp._internal.registry import Registry, RepoEntry


def test_main() -> None:
    """With no subcommand the CLI prints help and returns non-zero."""
    assert main([]) == 1


def test_show_help(capsys: pytest.CaptureFixture) -> None:
    """Show help.

    Parameters:
        capsys: Pytest fixture to capture output.
    """
    with pytest.raises(SystemExit):
        main(["-h"])
    captured = capsys.readouterr()
    assert "cheaphelp" in captured.out


def test_show_version(capsys: pytest.CaptureFixture) -> None:
    """Show version.

    Parameters:
        capsys: Pytest fixture to capture output.
    """
    with pytest.raises(SystemExit):
        main(["-V"])
    captured = capsys.readouterr()
    assert debug._get_version() in captured.out


def test_show_debug_info(capsys: pytest.CaptureFixture) -> None:
    """Show debug information.

    Parameters:
        capsys: Pytest fixture to capture output.
    """
    with pytest.raises(SystemExit):
        main(["--debug-info"])
    captured = capsys.readouterr().out.lower()
    assert "python" in captured
    assert "system" in captured
    assert "environment" in captured
    assert "packages" in captured


def test_repo_list_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`cheaphelp repo list --json` emits a JSON array with the required fields."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())  # make Workspace.exists() return True
    reg = Registry(ws.registry_path)
    reg.add(
        RepoEntry(owner="octocat", name="hello", default_branch="main", enabled=True),
    )
    reg.add(RepoEntry(owner="octocat", name="bye", default_branch="dev", enabled=False))

    rc = main(["--home", str(ws.home), "repo", "list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 2
    by_slug = {r["slug"]: r for r in data}
    assert set(by_slug) == {"octocat/hello", "octocat/bye"}
    for required in ("owner", "name", "slug", "default_branch", "enabled"):
        assert required in by_slug["octocat/hello"]
    assert by_slug["octocat/hello"]["enabled"] is True
    assert by_slug["octocat/bye"]["default_branch"] == "dev"
    assert by_slug["octocat/bye"]["enabled"] is False


def test_repo_list_text_default_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Without --json, the human-readable output is preserved; with --json, an empty list is `[]`."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())  # make Workspace.exists() return True
    reg = Registry(ws.registry_path)
    reg.add(RepoEntry(owner="octocat", name="hello"))

    # Default (no flag) — human-readable line with the repo slug.
    rc = main(["--home", str(ws.home), "repo", "list"])
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "octocat/hello" in text_out
    # The text path must not leak JSON braces.
    assert '{"owner"' not in text_out

    # Empty registry + --json -> `[]` (not the "No repositories registered" text).
    reg.remove("octocat", "hello")
    rc = main(["--home", str(ws.home), "repo", "list", "--json"])
    assert rc == 0
    json_out = capsys.readouterr().out
    assert json.loads(json_out) == []


# --- repo set --------------------------------------------------------------
def test_repo_set_updates_only_checks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --checks` updates only the checks field; autofix is preserved."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="old-checks",
            autofix="original-autofix",
        ),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "new-cmd",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Updated octocat/hello." in out

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "new-cmd"
    assert reloaded.autofix == "original-autofix"


def test_repo_set_updates_only_autofix(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --autofix` updates only the autofix field; checks is preserved."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="original-checks",
            autofix="old-autofix",
        ),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--autofix",
            "new-fixer",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Updated octocat/hello." in out

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "original-checks"
    assert reloaded.autofix == "new-fixer"


def test_repo_set_empty_clears_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --checks ""` clears the checks field; autofix is preserved."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="something",
            autofix="autofixer",
        ),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Updated octocat/hello." in out

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == ""
    assert reloaded.autofix == "autofixer"


def test_repo_set_unknown_repo_exits_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set` on a slug that isn't registered exits 1 and creates no file."""
    ws = _setup_workspace(tmp_path)
    # No registry file should exist yet — the failed set must not create it.
    assert not ws.registry_path.exists()

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "unknown/thing",
            "--checks",
            "x",
        ],
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown/thing is not registered." in err
    assert not ws.registry_path.exists()


def test_repo_set_no_flags_is_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set` with no flags is a no-op; the registry file is byte-identical."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="same-checks",
            autofix="same-autofix",
        ),
    )
    before_bytes = ws.registry_path.read_bytes()
    before_mtime_ns = ws.registry_path.stat().st_mtime_ns

    rc = main(["--home", str(ws.home), "repo", "set", "octocat/hello"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to update" in out

    after_bytes = ws.registry_path.read_bytes()
    after_mtime_ns = ws.registry_path.stat().st_mtime_ns
    # Byte-identical (and mtime untouched): a no-op must not touch the registry.
    assert before_bytes == after_bytes
    assert before_mtime_ns == after_mtime_ns

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "same-checks"
    assert reloaded.autofix == "same-autofix"


def test_repo_set_same_value_is_no_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --checks <same value>` reports "No changes" and leaves the registry file untouched."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="existing-cmd",
            autofix="existing-fix",
        ),
    )
    before_bytes = ws.registry_path.read_bytes()
    before_mtime_ns = ws.registry_path.stat().st_mtime_ns

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "existing-cmd",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "No changes for octocat/hello." in out
    # The old "Updated ..." message must not leak through for a no-change run.
    assert "Updated" not in out

    after_bytes = ws.registry_path.read_bytes()
    after_mtime_ns = ws.registry_path.stat().st_mtime_ns
    # Byte-identical (and mtime untouched): a no-op must not rewrite the registry.
    assert before_bytes == after_bytes
    assert before_mtime_ns == after_mtime_ns

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "existing-cmd"
    assert reloaded.autofix == "existing-fix"


def test_repo_set_list_reflects_update(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """After `repo set --checks`, `repo list --json` reports the new value."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(owner="octocat", name="hello", checks="old"),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "new",
        ],
    )
    assert rc == 0
    capsys.readouterr()  # discard set stdout

    rc = main(["--home", str(ws.home), "repo", "list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    by_slug = {r["slug"]: r for r in data}
    assert by_slug["octocat/hello"]["checks"] == "new"


# --- daily log file --------------------------------------------------------
def _setup_workspace(tmp_path: Path) -> Workspace:
    """Build a fresh, initialised workspace under ``tmp_path``."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())  # make Workspace.exists() return True
    return ws


def test_run_writes_daily_log_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cheaphelp run` writes a per-day log file mirroring every console line."""
    ws = _setup_workspace(tmp_path)

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        log("hello-from-stub")
        log("second line")
        return SimpleNamespace(error=None, total_turns=0, repos=[])

    monkeypatch.setattr(commands, "tick", fake_tick)

    rc = main(["--home", str(ws.home), "run"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "--- tick start ---" in captured
    assert "hello-from-stub" in captured
    assert "second line" in captured
    assert "Done." in captured

    expected = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    assert expected.exists()
    assert expected.is_file()
    contents = expected.read_text(encoding="utf-8")
    lines = contents.splitlines()

    # First line is a timestamped header in the expected format.
    assert lines, "log file should not be empty"
    header_re = re.compile(
        r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] --- tick start ---\s*$",
    )
    assert header_re.match(lines[0]), f"unexpected header line: {lines[0]!r}"

    # Sentinel lines appear in the expected order.
    assert "hello-from-stub" in contents
    assert "second line" in contents
    assert contents.index("hello-from-stub") < contents.index("second line")

    # Every non-empty line in the file also appears in stdout.
    for line in lines:
        if line:
            assert line in captured, f"file line not echoed to stdout: {line!r}"


def test_run_passes_max_issues_flag_to_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--max-issues` (CLI) overrides `max_issues_per_tick` (config) when > 0."""
    ws = _setup_workspace(tmp_path)

    captured: dict[str, object] = {}

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        captured["max_issues"] = max_issues
        return SimpleNamespace(error=None, total_turns=0, repos=[])

    monkeypatch.setattr(commands, "tick", fake_tick)

    # CLI flag is forwarded.
    assert main(["--home", str(ws.home), "run", "--max-issues", "2"]) == 0
    assert captured["max_issues"] == 2

    # No flag and no config => unlimited (0).
    assert main(["--home", str(ws.home), "run"]) == 0
    assert captured["max_issues"] == 0

    # Config value is honoured when no flag is given.
    ws.save_config(Config.from_dict({"max_issues_per_tick": 4}))
    assert main(["--home", str(ws.home), "run"]) == 0
    assert captured["max_issues"] == 4

    # CLI flag (> 0) overrides the config value.
    assert main(["--home", str(ws.home), "run", "--max-issues", "1"]) == 0
    assert captured["max_issues"] == 1


def test_run_appends_within_same_day(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running twice on the same day appends a second header + body to the daily log file."""
    ws = _setup_workspace(tmp_path)

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        log("hello-from-stub")
        return SimpleNamespace(error=None, total_turns=0, repos=[])

    monkeypatch.setattr(commands, "tick", fake_tick)

    rc1 = main(["--home", str(ws.home), "run"])
    assert rc1 == 0
    capsys.readouterr()  # discard first invocation's stdout

    log_path = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    assert log_path.exists()
    first_size = log_path.stat().st_size

    rc2 = main(["--home", str(ws.home), "run"])
    assert rc2 == 0

    second_size = log_path.stat().st_size
    assert second_size > first_size, "log file should grow when run again on the same day"

    contents = log_path.read_text(encoding="utf-8")
    assert contents.count("--- tick start ---") == 2
    assert contents.count("hello-from-stub") >= 2


def test_run_swallows_log_write_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log file that can't be written (e.g. path blocked by a directory) must not crash the tick."""
    ws = _setup_workspace(tmp_path)
    blocker = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    blocker.mkdir()  # opening this path for writing raises IsADirectoryError (an OSError)
    try:

        def fake_tick(
            workspace: Workspace,
            *,
            dry_run: bool,
            log: Callable[[str], None],
            max_issues: int = 0,
        ) -> SimpleNamespace:
            log("would-be-logged")
            return SimpleNamespace(error=None, total_turns=0, repos=[])

        monkeypatch.setattr(commands, "tick", fake_tick)

        rc = main(["--home", str(ws.home), "run"])
        assert rc == 0, "tick must not crash on a log write failure"

        captured = capsys.readouterr().out
        assert "--- tick start ---" in captured
        assert "would-be-logged" in captured
    finally:
        blocker.rmdir()


# --- status ----------------------------------------------------------------
# Test-only token string written into the workspace `.env` by the status
# tests. A real `GITHUB_TOKEN` is never read or sent anywhere in tests; this
# value just has to be non-empty so the command does not exit on the no-token
# branch.
_TEST_TOKEN = "test-token"


class _FakeGH:
    """Stand-in for ``commands.GitHubClient`` used by the status tests.

    The fake holds per-repo issue lists and per-issue comment lists in plain
    dicts so tests can seed exactly the data the command should consume.
    """

    def __init__(self, token: str, **kwargs: object) -> None:
        self.token = token
        self.kwargs = kwargs
        self.login = "mybot"
        self.issues: dict[str, list[Issue]] = {}
        self.comments: dict[tuple[str, int], list[Comment]] = {}
        self.instantiated = False

    def __enter__(self) -> _FakeGH:
        self.instantiated = True
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def authenticated_login(self) -> str:
        return self.login

    def list_open_issues(self, owner: str, name: str) -> list[Issue]:
        return list(self.issues.get(f"{owner}/{name}", []))

    def list_issue_comments(self, owner: str, name: str, number: int) -> list[Comment]:
        return list(self.comments.get((f"{owner}/{name}", number), []))


def _seed_workspace_env(ws: Workspace, *, token: str) -> None:
    """Write a ``GITHUB_TOKEN`` into the workspace ``.env`` file."""
    update_env_file(ws.env_path, {GITHUB_TOKEN_KEY: token})


def test_status_happy_path_groups_and_stages(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One enabled repo's issues are grouped under its slug with the right stage per row."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    reg = Registry(ws.registry_path)
    reg.add(RepoEntry(owner="octocat", name="hello", enabled=True))
    reg.add(RepoEntry(owner="octocat", name="bye", enabled=False))

    planned_label = Config().labels["planned"]
    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=1,
            title="Issue with planned label",
            body="",
            state="open",
            labels=[planned_label],
            user="alice",
            html_url="",
        ),
        Issue(
            number=2,
            title="Issue with no labels",
            body="",
            state="open",
            labels=[],
            user="alice",
            html_url="",
        ),
    ]

    # Reach into the fake construction: we need the seeded instance to be the
    # one cmd_status actually receives. Re-bind via a small factory shim.
    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "octocat/hello" in captured
    assert "octocat/bye" not in captured
    assert "#1" in captured
    assert "#2" in captured

    planned_line = next(line for line in captured.splitlines() if "Issue with planned label" in line)
    assert "build" in planned_line

    other_line = next(line for line in captured.splitlines() if "Issue with no labels" in line)
    assert "responder" in other_line


def test_status_in_review_label_shows_in_review_stage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issues with the `in-review` label are rendered with the literal stage name `in-review`."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    in_review_label = Config().labels["in_review"]
    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=1,
            title="Terminal issue",
            body="",
            state="open",
            labels=[in_review_label],
            user="alice",
            html_url="",
        ),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr()
    issue_line = next(line for line in captured.out.splitlines() if "#1" in line)
    # The stage column shows the literal stage name, not a dash or the word "None".
    assert "in-review" in issue_line
    # Defensive: guard against a future regression that leaks None back into the column.
    assert "None" not in issue_line


def test_status_idle_stage_renders_idle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh issue (no labels) whose last comment is from the bot renders as `idle`."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=1,
            title="Quiet issue",
            body="",
            state="open",
            labels=[],
            user="alice",
            html_url="",
        ),
    ]
    # Bot authored the last comment, so the responder is not waiting on anything.
    fake.comments[("octocat/hello", 1)] = [
        Comment(id=1, body="hi", user=fake.login, created_at=""),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr()
    issue_line = next(line for line in captured.out.splitlines() if "#1" in line)
    assert "idle" in issue_line
    assert "None" not in issue_line


def test_status_truncates_long_titles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Titles longer than the width column are truncated to 60 chars ending with `…`."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    long_title = "A" * 100
    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=1,
            title=long_title,
            body="",
            state="open",
            labels=[],
            user="alice",
            html_url="",
        ),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    ellipsis = "\u2026"

    truncated_lines = [line for line in captured.splitlines() if ellipsis in line]
    assert len(truncated_lines) == 1, f"expected exactly one truncated row, got: {truncated_lines!r}"
    line = truncated_lines[0]

    # The title column sits between the number column and a 2-space gap before
    # the stage; it's formatted to 60 chars and ends with the ellipsis.
    match = re.match(r"^  #\d+ +(?P<title>.{60})  (?P<stage>\S.*)$", line)
    assert match is not None, f"line did not match expected format: {line!r}"
    assert match.group("title").endswith(ellipsis)
    assert len(match.group("title")) == 60

    # The full 100-char title must not appear in stdout (i.e. truncation ran).
    assert long_title not in captured


def test_status_repo_with_no_open_issues(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled repos with zero open issues print a placeholder marker."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    fake = _FakeGH("test-token")
    # No issues seeded for octocat/hello.

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "octocat/hello" in captured
    assert "(no open issues)" in captured


def test_status_disabled_repos_are_skipped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled repos are never listed, even if they have open issues."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(
        RepoEntry(owner="octocat", name="off", enabled=False),
    )

    fake = _FakeGH("test-token")
    fake.issues["octocat/off"] = [
        Issue(
            number=1,
            title="Should be hidden",
            body="",
            state="open",
            labels=[],
            user="alice",
            html_url="",
        ),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "octocat/off" not in captured


def test_status_no_enabled_repos(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only disabled repos, status prints the empty-state message and exits 0."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(
        RepoEntry(owner="octocat", name="off", enabled=False),
    )

    def _factory(token: str, **_kwargs: object) -> _FakeGH:  # pragma: no cover - never reached
        return _FakeGH(token)

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "No enabled repositories registered" in captured


def test_status_no_token_exits_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``GITHUB_TOKEN`` exits 1 with a stderr message; no client is created."""
    ws = _setup_workspace(tmp_path)
    # Deliberately do NOT seed the env file.
    # Also wipe any inherited env var from previous tests in this process.
    monkeypatch.delenv(GITHUB_TOKEN_KEY, raising=False)

    # If the no-token branch is bypassed by mistake, this would raise and fail
    # the test loudly (no real HTTP call is possible).
    def _exploding_factory(_token: str, **_kwargs: object) -> _FakeGH:
        msg = "GitHubClient should not be instantiated when GITHUB_TOKEN is missing"
        raise AssertionError(msg)

    monkeypatch.setattr(commands, "GitHubClient", _exploding_factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 1
    assert GITHUB_TOKEN_KEY in capsys.readouterr().err


def test_status_no_workspace_exits_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no initialised workspace the command exits 1 and never instantiates a client."""
    ws = Workspace(tmp_path)  # NOTE: no ws.ensure() / ws.save_config()

    def _exploding_factory(_token: str, **_kwargs: object) -> _FakeGH:
        msg = "GitHubClient should not be instantiated without a workspace"
        raise AssertionError(msg)

    monkeypatch.setattr(commands, "GitHubClient", _exploding_factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No workspace" in err


# --- clean -----------------------------------------------------------------
def test_cmd_clean_removes_closed_and_orphan_clones(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`clean` removes build clones for closed issues and unregistered repos."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    for name in ("octocat__hello", "octocat__hello__issue-1", "octocat__hello__issue-2", "ghost__repo"):
        (ws.clones_dir / name / ".git").mkdir(parents=True)

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(number=2, title="t", body="", state="open", labels=[], user="a", html_url=""),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "clean"])
    assert rc == 0
    assert not (ws.clones_dir / "octocat__hello__issue-1").exists()  # closed -> removed
    assert (ws.clones_dir / "octocat__hello__issue-2").exists()  # open -> kept
    assert (ws.clones_dir / "octocat__hello").exists()  # shared -> kept
    assert not (ws.clones_dir / "ghost__repo").exists()  # unregistered -> removed
    assert "Removed" in capsys.readouterr().out


# --- per-issue lock skip behaviour -----------------------------------------
def test_cmd_run_skips_locked_issue_but_completes_tick(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked issue is skipped (logged) while the tick still runs to completion.

    Ticks no longer take a single global lock; instead each issue has its own
    lock so an overlapping tick can work on other issues. Holding one issue's
    lock makes `run` report it as in progress elsewhere, but the tick proceeds.
    """
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))
    # Mock mode: _is_mock() True so the tick never clones or calls an agent.
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", "/dev/null")

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(number=7, title="t", body="", state="open", labels=[], user="alice", html_url=""),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(orchestrator, "GitHubClient", _factory)

    with RunLock(ws.issue_lock_path("octocat", "hello", 7)) as holder:
        assert holder.acquired
        rc = main(["--home", str(ws.home), "run"])
    assert rc == 0

    combined = (capsys.readouterr().out).lower()
    # The tick ran (it authenticated) and finished with a summary...
    assert "acting as" in combined
    assert "done." in combined
    # ...but issue #7 was skipped because its lock was held.
    assert "#7" in combined
    assert "skipping" in combined


# --- logs -------------------------------------------------------------------
def _today_log_path(ws: Workspace) -> Path:
    """Return the expected daily log path for today."""
    return ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"


def test_logs_no_log_file_today_prints_message_and_exits_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When today's log does not exist, ''logs'' prints a stderr message and exits 0."""
    ws = _setup_workspace(tmp_path)
    # Ensure the logs dir exists but contains no file for today.
    ws.logs_dir.mkdir(parents=True, exist_ok=True)
    today_path = _today_log_path(ws)
    assert not today_path.exists()

    rc = main(["--home", str(ws.home), "logs"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "(no log for today" in captured.err
    assert captured.out.strip() == ""


def test_logs_prints_tail_of_today_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """With a populated log file, ''logs'' prints the tail in file order."""
    ws = _setup_workspace(tmp_path)
    log_path = _today_log_path(ws)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "[2026-06-14 14:30:00] --- tick start ---",
        "  \u00b7 octocat/hello#1: responding \u2026",
        "  \u00b7 octocat/hello#1: running planner \u2026",
        "[2026-06-14 14:31:00] --- tick start ---",
        "  \u00b7 octocat/hello#2: responding \u2026",
        "  \u00b7 octocat/hello#3: responding \u2026",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["--home", str(ws.home), "logs"])
    assert rc == 0
    out = capsys.readouterr().out
    out_lines = out.splitlines()

    # Every written line appears in the output (since < 50 lines = the tail).
    for line in lines:
        assert line in out, f"missing expected line: {line!r}"
    # Order in output matches order in the file.
    for i in range(len(lines)):
        assert out_lines[i] == lines[i], f"line {i} mismatch: {out_lines[i]!r} != {lines[i]!r}"


def test_logs_issue_filter_returns_only_matching_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """''--issue N'' filters to lines containing ''#N'' but still includes headers."""
    ws = _setup_workspace(tmp_path)
    log_path = _today_log_path(ws)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "[2026-06-14 14:30:00] --- tick start ---",
        "  \u00b7 octocat/hello#7: planner",
        "  \u00b7 octocat/hello#8: planner",
        "[2026-06-14 14:31:00] --- tick start ---",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["--home", str(ws.home), "logs", "--issue", "7"])
    assert rc == 0
    out = capsys.readouterr().out

    # Only lines containing "#7" pass the filter (f"#{n}" in line).
    assert "octocat/hello#7" in out
    # The #8 line must NOT appear (filtered out).
    assert "octocat/hello#8" not in out
    # Headers without any issue ref are also filtered out.
    assert "--- tick start ---" not in out


def test_logs_follow_exits_cleanly_on_keyboard_interrupt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """''--follow'' exits 0 when ''time.sleep'' is interrupted by Ctrl-C."""
    ws = _setup_workspace(tmp_path)
    log_path = _today_log_path(ws)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("one line\n", encoding="utf-8")

    # Fake time.sleep to raise KeyboardInterrupt on first call.
    def _sleep_that_raises(_secs: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "time", SimpleNamespace(sleep=_sleep_that_raises))

    args = SimpleNamespace(home=str(ws.home), follow=True, issue=None)
    rc = commands.cmd_logs(args)
    assert rc == 0


# --- blast-radius CLI flags ------------------------------------------------
def test_repo_add_stores_max_diff_files_and_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo add --max-diff-files 50 --max-diff-lines 2000` stores both fields."""
    ws = _setup_workspace(tmp_path)
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "add",
            "octocat/hello",
            "--max-diff-files",
            "50",
            "--max-diff-lines",
            "2000",
        ],
    )
    assert rc == 0
    capsys.readouterr()  # discard output

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 50
    assert entry.max_diff_lines == 2000


def test_repo_add_default_limits_when_unspecified(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo add` without flags uses the RepoEntry defaults (30, 1000)."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "repo", "add", "octocat/hello"])
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 30
    assert entry.max_diff_lines == 1000


def test_repo_update_updates_max_diff_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo update --max-diff-files 100` changes only that field."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="ruff check",
            autofix="ruff format",
            max_diff_files=30,
            max_diff_lines=1000,
        ),
    )
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "update",
            "octocat/hello",
            "--max-diff-files",
            "100",
        ],
    )
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 100
    assert entry.max_diff_lines == 1000
    assert entry.checks == "ruff check"
    assert entry.autofix == "ruff format"


def test_repo_update_updates_max_diff_lines_and_preserves_others(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo update --max-diff-lines 0` sets unlimited, preserves other fields."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="ruff check",
            autofix="ruff format",
            max_diff_files=30,
            max_diff_lines=1000,
        ),
    )
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "update",
            "octocat/hello",
            "--max-diff-lines",
            "0",
        ],
    )
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_lines == 0
    assert entry.max_diff_files == 30
    assert entry.checks == "ruff check"
    assert entry.autofix == "ruff format"


def test_repo_set_with_new_flags_still_works(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --max-diff-files 75` forwards the flag correctly."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="old-checks",
            autofix="old-autofix",
        ),
    )
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--max-diff-files",
            "75",
        ],
    )
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 75
    assert entry.max_diff_lines == 1000
    assert entry.checks == "old-checks"
    assert entry.autofix == "old-autofix"
