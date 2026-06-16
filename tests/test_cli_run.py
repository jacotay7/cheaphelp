"""Tests for cheaphelp's `run` subcommand."""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cheaphelp import main
from cheaphelp._internal import commands, orchestrator
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import Issue
from cheaphelp._internal.lock import RunLock
from cheaphelp._internal.registry import Registry, RepoEntry
from tests.conftest import _TEST_TOKEN, _FakeGH, _seed_workspace_env, _setup_workspace, _usage_data

# --- daily log file --------------------------------------------------------


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


def test_run_shows_cost_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost summary is printed to stdout and the daily log when costs are non-zero."""
    ws = _setup_workspace(tmp_path)

    ud = _usage_data(prompt_tokens=1234, completion_tokens=567, total_tokens=1801, cost_usd=0.042)
    repo = SimpleNamespace(
        slug="octocat/hello",
        issue_costs={
            7: {
                "responder": [_usage_data(prompt_tokens=200, completion_tokens=80, total_tokens=280, cost_usd=0.002)],
                "planner": [_usage_data(prompt_tokens=400, completion_tokens=200, total_tokens=600, cost_usd=0.008)],
                "worker": [
                    _usage_data(prompt_tokens=400, completion_tokens=200, total_tokens=600, cost_usd=0.019),
                    _usage_data(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=0.0),
                ],
                "reviewer": [_usage_data(prompt_tokens=234, completion_tokens=87, total_tokens=321, cost_usd=0.002)],
            },
        },
        cost=ud,
    )

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        log("hello-from-stub")
        return SimpleNamespace(error=None, total_turns=3, repos=[repo], total_cost=ud)

    monkeypatch.setattr(commands, "tick", fake_tick)

    rc = main(["--home", str(ws.home), "run"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "Cost: $0.042 (1,234 prompt + 567 completion tokens)" in captured
    assert (
        "octocat/hello#7:   $0.031  (responder $0.002, planner $0.008, worker x2 $0.019, reviewer $0.002)" in captured
    )

    # Also check the daily log contains the cost lines.
    log_path = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    assert log_path.exists()
    log_contents = log_path.read_text(encoding="utf-8")
    assert "Cost: $0.042 (1,234 prompt + 567 completion tokens)" in log_contents
    assert (
        "octocat/hello#7:   $0.031  (responder $0.002, planner $0.008, worker x2 $0.019, reviewer $0.002)"
        in log_contents
    )


def test_run_no_cost_when_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With zero costs (default UsageData), no cost line is emitted to stdout or log."""
    ws = _setup_workspace(tmp_path)

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        log("hello-from-stub")
        return SimpleNamespace(error=None, total_turns=0, repos=[], total_cost=_usage_data())

    monkeypatch.setattr(commands, "tick", fake_tick)

    rc = main(["--home", str(ws.home), "run"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "Cost:" not in captured

    log_path = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    if log_path.exists():
        log_contents = log_path.read_text(encoding="utf-8")
        assert "Cost:" not in log_contents


# --- multi-tick run ---------------------------------------------------------
def test_run_help_lists_new_flags_and_drops_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """``run -h`` shows the new flags and no longer lists ``--once``."""
    ws = _setup_workspace(tmp_path)
    with pytest.raises(SystemExit):
        main(["--home", str(ws.home), "run", "-h"])
    out = capsys.readouterr().out
    assert "--num-ticks" in out
    assert "--continuous" in out
    assert "--max-ticks" in out
    assert "--sleep" in out
    assert "--dry-run" in out
    assert "-n N" in out
    assert "--once" not in out


def test_run_rejects_removed_once_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Passing ``--once`` exits with code 2 (the flag no longer exists)."""
    ws = _setup_workspace(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["--home", str(ws.home), "run", "--once"])
    assert exc_info.value.code == 2


def test_run_num_ticks_runs_n_ticks_with_sleep(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-n 3 --sleep 1`` runs the tick three times, sleeping between each."""
    ws = _setup_workspace(tmp_path)

    tick_count: list[int] = []
    sleep_records: list[float] = []

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        tick_count.append(len(tick_count) + 1)
        log(f"[tick {tick_count[-1]}/3]")
        return SimpleNamespace(error=None, total_turns=1, repos=[], total_cost=_usage_data())

    def record_sleep(secs: float) -> None:
        sleep_records.append(secs)

    monkeypatch.setattr(commands, "tick", fake_tick)
    monkeypatch.setattr(commands, "time", SimpleNamespace(sleep=record_sleep))

    rc = main(["--home", str(ws.home), "run", "-n", "3", "--sleep", "1"])
    assert rc == 0
    assert len(tick_count) == 3, f"expected 3 ticks, got {len(tick_count)}"

    # Sleep is called between ticks; 3 ticks => 2 sleeps.
    assert len(sleep_records) == 2, f"expected 2 sleep calls, got {len(sleep_records)}"
    for s in sleep_records:
        assert s == 1.0, f"expected 1.0s sleep, got {s}"

    captured = capsys.readouterr().out
    assert "3 tick(s)" in captured
    assert "[tick 1/3]" in captured
    assert "[tick 2/3]" in captured
    assert "[tick 3/3]" in captured

    # The daily log file contains the tick headers.
    log_path = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    assert log_path.exists()
    log_contents = log_path.read_text(encoding="utf-8")
    assert log_contents.count("--- tick") == 3


def test_run_continuous_stops_on_idle_tick(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--continuous`` stops after the first tick with zero turns."""
    ws = _setup_workspace(tmp_path)

    call_order: list[int] = []

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        idx = len(call_order) + 1
        call_order.append(idx)
        # Return work for first 2 calls, idle from 3rd onward.
        turns = 1 if idx <= 2 else 0
        return SimpleNamespace(error=None, total_turns=turns, repos=[], total_cost=_usage_data())

    def noop_sleep(secs: float) -> None:
        return

    monkeypatch.setattr(commands, "tick", fake_tick)
    monkeypatch.setattr(commands, "time", SimpleNamespace(sleep=noop_sleep))

    rc = main(["--home", str(ws.home), "run", "--continuous", "--max-ticks", "5", "--sleep", "0.001"])
    assert rc == 0
    # 3 ticks: work on 1 and 2, idle on 3 (break).
    assert len(call_order) == 3, f"expected 3 ticks, got {len(call_order)}"

    captured = capsys.readouterr().out
    assert "3 tick(s)" in captured


def test_run_continuous_caps_at_max_ticks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--continuous`` with always-busy ticks stops at ``--max-ticks``."""
    ws = _setup_workspace(tmp_path)

    call_order: list[int] = []
    sleep_records: list[float] = []

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        idx = len(call_order) + 1
        call_order.append(idx)
        return SimpleNamespace(error=None, total_turns=1, repos=[], total_cost=_usage_data())

    def record_sleep(secs: float) -> None:
        sleep_records.append(secs)

    monkeypatch.setattr(commands, "tick", fake_tick)
    monkeypatch.setattr(commands, "time", SimpleNamespace(sleep=record_sleep))

    rc = main(
        ["--home", str(ws.home), "run", "--continuous", "--max-ticks", "4", "--sleep", "0.001"],
    )
    assert rc == 0
    # Max is 4, all ticks return work -> 4 ticks.
    assert len(call_order) == 4, f"expected 4 ticks, got {len(call_order)}"
    # 4 ticks => 3 sleeps between them.
    assert len(sleep_records) == 3, f"expected 3 sleep calls, got {len(sleep_records)}"


def test_run_continuous_and_num_ticks_are_mutually_exclusive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Combining ``--continuous`` and ``-n`` exits with code 2."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "run", "--continuous", "-n", "3"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--continuous" in err
    assert "--num-ticks" in err


def test_run_default_is_one_tick(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cheaphelp run`` with no flags runs exactly one tick."""
    ws = _setup_workspace(tmp_path)

    tick_count: list[int] = []

    def fake_tick(
        workspace: Workspace,
        *,
        dry_run: bool,
        log: Callable[[str], None],
        max_issues: int = 0,
    ) -> SimpleNamespace:
        tick_count.append(len(tick_count) + 1)
        return SimpleNamespace(error=None, total_turns=0, repos=[], total_cost=_usage_data())

    monkeypatch.setattr(commands, "tick", fake_tick)

    rc = main(["--home", str(ws.home), "run"])
    assert rc == 0
    assert len(tick_count) == 1, f"expected 1 tick, got {len(tick_count)}"


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
        Issue(
            number=7, title="t", body="", state="open", labels=[Config().labels["activated"]], user="alice", html_url="",
        ),
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
