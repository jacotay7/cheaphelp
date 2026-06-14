"""Tests for cheaphelp's `logs` subcommand."""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cheaphelp import main
from cheaphelp._internal import commands
from cheaphelp._internal.config import Workspace
from tests.conftest import _setup_workspace


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

    args = argparse.Namespace(home=str(ws.home), follow=True, issue=None)
    rc = commands.cmd_logs(args)
    assert rc == 0
