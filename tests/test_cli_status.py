"""Tests for cheaphelp's `status` subcommand."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cheaphelp import main
from cheaphelp._internal import commands, opencode
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.env import GITHUB_TOKEN_KEY
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.registry import Registry, RepoEntry
from cheaphelp._internal.responder import BOT_MARKER
from cheaphelp._internal.spend import DailySpendTracker
from tests.conftest import _TEST_TOKEN, _FakeGH, _seed_workspace_env, _setup_workspace

# --- status ----------------------------------------------------------------


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
    activated_label = Config().labels["activated"]
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
            title="Issue with activation label (no pipeline labels)",
            body="",
            state="open",
            labels=[activated_label],
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

    other_line = next(line for line in captured.splitlines() if "Issue with activation label" in line)
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
    assert "rework" in issue_line
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
        Comment(id=1, body=f"{BOT_MARKER}\nhi", user=fake.login, created_at=""),
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


def test_status_shows_costs_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cheaphelp status --costs`` shows the cumulative cost for each issue."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    # Seed a cost.json for issue #7.
    issue_dir = ws.issue_dir("octocat", "hello", 7)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "cost.json").write_text(
        json.dumps({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost_usd": 0.0123}),
        encoding="utf-8",
    )

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=7,
            title="Costly issue",
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

    rc = main(["--home", str(ws.home), "status", "--costs"])
    assert rc == 0

    captured = capsys.readouterr().out
    # The issue line must include the dollar amount.
    assert "$0.012" in captured
    assert "#7" in captured


def test_status_no_costs_when_flag_absent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--costs``, the output has no cost column (no ``$`` on issue lines)."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=7,
            title="Normal issue",
            body="",
            state="open",
            labels=[],
            user="alice",
            html_url="",
        ),
    ]
    fake.comments[("octocat/hello", 7)] = [
        Comment(id=1, body="hi", user=fake.login, created_at=""),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    # Issue line should not contain a dollar sign (no cost column).
    issue_lines = [line for line in captured.splitlines() if "#7" in line]
    assert issue_lines
    assert "$" not in issue_lines[0]


def test_status_costs_zero_when_no_cost_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cheaphelp status --costs`` on an issue with no cost.json shows $0.000 (no crash)."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))
    # No cost.json written.

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=7,
            title="No cost file",
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

    rc = main(["--home", str(ws.home), "status", "--costs"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "$0.000" in captured
    assert "#7" in captured


# --- budget footer -----------------------------------------------------------


def test_status_budget_line_shows_spend_and_cap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget line shows formatted spend and cap when a cap is configured."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    ws.save_config(Config.from_dict({"daily_budget_usd": 5.0}))

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    DailySpendTracker(ws.state_dir).record(opencode.UsageData(cost_usd=1.23))

    fake = _FakeGH("test-token")

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    non_empty = [line for line in captured.splitlines() if line.strip()]
    assert non_empty[-1] == "Budget: $1.230 / $5.000 daily cap"


def test_status_budget_line_exhausted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget line shows exhausted variant when spend meets or exceeds cap."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    ws.save_config(Config.from_dict({"daily_budget_usd": 1.0}))

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    DailySpendTracker(ws.state_dir).record(opencode.UsageData(cost_usd=1.5))

    fake = _FakeGH("test-token")

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    non_empty = [line for line in captured.splitlines() if line.strip()]
    assert non_empty[-1] == "Budget: EXHAUSTED — spent $1.500 of $1.000 daily cap. Resumes tomorrow (UTC)."


def test_status_budget_line_omitted_when_cap_is_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget line is omitted entirely when daily_budget_usd is 0 (unlimited)."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    # Default config has daily_budget_usd = 0.0; do not override it.

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    fake = _FakeGH("test-token")

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "Budget:" not in captured


def test_status_budget_line_zero_spend_with_no_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget line shows zero spend when daily_spend.json does not exist."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    ws.save_config(Config.from_dict({"daily_budget_usd": 5.0}))
    # Do NOT create daily_spend.json.

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    fake = _FakeGH("test-token")

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    non_empty = [line for line in captured.splitlines() if line.strip()]
    assert non_empty[-1] == "Budget: $0.000 / $5.000 daily cap"


def test_status_budget_line_zero_spend_after_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget line shows zero spend after recording zero-cost usage (tracking active)."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    ws.save_config(Config.from_dict({"daily_budget_usd": 5.0}))

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    # Record zero-cost usage — creates the file.
    DailySpendTracker(ws.state_dir).record(opencode.UsageData(cost_usd=0.0))

    fake = _FakeGH("test-token")

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "Budget: $0.000 / $5.000 daily cap" in captured


def test_status_budget_line_is_footer_after_empty_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget line appears after the 'no enabled repos' empty-state message and is last."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    ws.save_config(Config.from_dict({"daily_budget_usd": 5.0}))

    Registry(ws.registry_path).add(
        RepoEntry(owner="octocat", name="off", enabled=False),
    )

    # No enabled repos — GitHubClient is never instantiated.
    def _exploding_factory(_token: str, **_kwargs: object) -> _FakeGH:
        msg = "GitHubClient should not be called when no repos are enabled"
        raise AssertionError(msg)

    monkeypatch.setattr(commands, "GitHubClient", _exploding_factory)

    rc = main(["--home", str(ws.home), "status"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "No enabled repositories registered" in captured
    assert "Budget: $0.000 / $5.000 daily cap" in captured
    non_empty = [line for line in captured.splitlines() if line.strip()]
    assert non_empty[-1] == "Budget: $0.000 / $5.000 daily cap"


def test_status_budget_line_is_footer_after_repo_listing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget line appears after the per-repo issue listing and is the last non-empty line."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    ws.save_config(Config.from_dict({"daily_budget_usd": 5.0}))

    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(
            number=1,
            title="Open issue",
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
    assert "octocat/hello" in captured
    assert "#1" in captured
    non_empty = [line for line in captured.splitlines() if line.strip()]
    assert non_empty[-1] == "Budget: $0.000 / $5.000 daily cap"
