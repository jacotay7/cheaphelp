"""Tests for cheaphelp's systemd unit/timer generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cheaphelp._internal import (
    systemd,
)


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
    assert "--once" not in units.service, "service must not use the removed --once flag"
    assert "-m cheaphelp run" in units.service
    assert f"CHEAPHELP_HOME={tmp_path}" in units.service


def test_render_units_continuous_by_default(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m")
    assert units.service.rstrip().endswith("-m cheaphelp run --continuous --max-ticks 20 --sleep 30")


def test_render_units_continuous_options(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m", max_ticks=5, sleep=10)
    assert "--continuous --max-ticks 5 --sleep 10" in units.service


def test_render_units_no_continuous(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m", continuous=False)
    assert units.service.rstrip().endswith("-m cheaphelp run")
    assert "--continuous" not in units.service


# --- status() ---------------------------------------------------------------

TIMER_LINE = (
    "Mon 2025-01-01 00:00:00 UTC  n/a           "
    "Mon 2025-01-01 00:00:00 UTC  n/a           "
    "cheaphelp.timer              cheaphelp.service"
)
SHOW_STDOUT = (
    "ActiveState=inactive\nResult=success\nExecMainStatus=0\nActiveEnterTimestamp=Mon 2025-01-01 00:00:00 UTC\n"
)


def test_status_timer_only_when_service_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service unit not installed => show timer + not-found + no-journal."""
    timers_out = (
        "NEXT                        LEFT          LAST                        PASSED       UNIT                ACTIVATES\n"
        f"{TIMER_LINE}\n"
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "list-timers":
            return SimpleNamespace(returncode=0, stdout=timers_out, stderr="")
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "show":
            return SimpleNamespace(returncode=1, stdout="", stderr="unit not loaded")
        return SimpleNamespace(returncode=1, stdout="", stderr="-- No entries --")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    output = systemd.status()

    assert TIMER_LINE in output
    assert "cheaphelp.service not found" in output
    assert "no journal available" in output


def test_status_includes_show_and_journal_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three subprocess calls succeed => full output."""
    timers_out = (
        "NEXT                        LEFT          LAST                        PASSED       UNIT                ACTIVATES\n"
        f"{TIMER_LINE}\n"
    )
    journal_out = "line1\nline2\nline3\n"

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "list-timers":
            return SimpleNamespace(returncode=0, stdout=timers_out, stderr="")
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "show":
            return SimpleNamespace(returncode=0, stdout=SHOW_STDOUT, stderr="")
        return SimpleNamespace(returncode=0, stdout=journal_out, stderr="")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    output = systemd.status()

    assert TIMER_LINE in output
    assert "ActiveState=inactive" in output
    assert "Result=success" in output
    assert "ExecMainStatus=0" in output
    assert "ActiveEnterTimestamp=Mon 2025-01-01 00:00:00 UTC" in output
    assert "line1" in output
    assert "line2" in output
    assert "line3" in output


def test_status_journal_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journalctl fails => show section present, journal placeholder shown."""
    timers_out = (
        "NEXT                        LEFT          LAST                        PASSED       UNIT                ACTIVATES\n"
        f"{TIMER_LINE}\n"
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "list-timers":
            return SimpleNamespace(returncode=0, stdout=timers_out, stderr="")
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "show":
            return SimpleNamespace(returncode=0, stdout=SHOW_STDOUT, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="-- No entries --")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    output = systemd.status()

    assert "ActiveState=inactive" in output
    assert "Result=success" in output
    assert "ExecMainStatus=0" in output
    assert "ActiveEnterTimestamp=Mon 2025-01-01 00:00:00 UTC" in output
    assert "no journal available" in output


def test_status_journal_empty_treated_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journalctl returns empty stdout => treated same as unavailable."""
    timers_out = (
        "NEXT                        LEFT          LAST                        PASSED       UNIT                ACTIVATES\n"
        f"{TIMER_LINE}\n"
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "list-timers":
            return SimpleNamespace(returncode=0, stdout=timers_out, stderr="")
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "show":
            return SimpleNamespace(returncode=0, stdout=SHOW_STDOUT, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    output = systemd.status()

    assert "no journal available" in output
    assert "Result=success" in output


def test_status_list_timers_failure_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list-timers fails => legacy error message still shown."""

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "list-timers":
            return SimpleNamespace(returncode=1, stdout="", stderr="dbus unavailable")
        if cmd[:2] == ["systemctl", "--user"] and cmd[2] == "show":
            return SimpleNamespace(returncode=0, stdout=SHOW_STDOUT, stderr="")
        return SimpleNamespace(returncode=0, stdout="some log", stderr="")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    output = systemd.status()

    assert "could not query timers:" in output
    assert "dbus unavailable" in output


def test_journalctl_helper_uses_correct_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_journalctl prepends ['journalctl', '--user'] to its args."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    systemd._journalctl("-u", "foo.service", "-n", "5", "--no-pager")

    assert captured == [["journalctl", "--user", "-u", "foo.service", "-n", "5", "--no-pager"]]
