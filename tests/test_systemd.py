"""Tests for cheaphelp's systemd unit/timer generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from cheaphelp._internal import (
    systemd,
)
from cheaphelp._internal.config import Workspace


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
    assert "cheaphelp run" in units.service
    assert f"CHEAPHELP_HOME={tmp_path}" in units.service


def test_render_units_continuous_by_default(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m")
    assert units.service.rstrip().endswith("cheaphelp run --continuous --max-ticks 20 --sleep 30")


def test_render_units_continuous_options(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m", max_ticks=5, sleep=10)
    assert "--continuous --max-ticks 5 --sleep 10" in units.service


def test_render_units_no_continuous(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m", continuous=False)
    assert units.service.rstrip().endswith("cheaphelp run")
    assert "--continuous" not in units.service


# --- install --linger -------------------------------------------------------
def _setup_workspace(tmp_path: Path) -> Workspace:
    """Build a fresh, initialised workspace under ``tmp_path``."""
    from cheaphelp._internal.config import Config  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())
    return ws


def _patch_systemctl_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``systemd._systemctl`` to always succeed."""

    def fake(*_a: str) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd, "_systemctl", fake)


def _patch_loginctl_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``systemd._loginctl`` to always succeed."""

    def fake(*_a: str) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd, "_loginctl", fake)


def _patch_unit_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``systemd.user_unit_dir`` into ``tmp_path``.

    ``install()`` writes unit files before any of the (mocked) systemctl/
    loginctl calls, so without this every install test would write real
    files into the developer's actual ``~/.config/systemd/user/``.
    """
    monkeypatch.setattr(systemd, "user_unit_dir", lambda: tmp_path / "systemd-user")


def test_install_linger_calls_loginctl_with_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``install(linger=True)`` calls ``loginctl enable-linger <user>``."""
    _patch_systemctl_success(monkeypatch)
    _patch_loginctl_success(monkeypatch)
    _patch_unit_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("USER", "tester")

    actions = systemd.install(home=tmp_path, interval="10m", linger=True)
    assert "enabled lingering for tester" in actions


def test_install_linger_default_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``install(linger=False)`` does not call ``loginctl``."""
    _patch_systemctl_success(monkeypatch)
    _patch_unit_dir(monkeypatch, tmp_path)

    loginctl_calls: list[tuple[str, ...]] = []

    def record_loginctl(*args: str) -> SimpleNamespace:
        loginctl_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd, "_loginctl", record_loginctl)
    monkeypatch.setenv("USER", "tester")

    actions = systemd.install(home=tmp_path, interval="10m", linger=False)
    assert not loginctl_calls
    assert not any("enabled lingering for" in a for a in actions)


def test_install_linger_handles_loginctl_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``loginctl`` returning non-zero logs a warning and continues."""
    _patch_unit_dir(monkeypatch, tmp_path)
    systemctl_calls: list[tuple[str, ...]] = []

    def record_systemctl(*args: str) -> SimpleNamespace:
        systemctl_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd, "_systemctl", record_systemctl)

    def fail_loginctl(*_a: str) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="no such user")

    monkeypatch.setattr(systemd, "_loginctl", fail_loginctl)
    monkeypatch.setenv("USER", "tester")

    actions = systemd.install(home=tmp_path, interval="10m", linger=True)

    # Lingering failure is logged as a warning.
    assert any(a.startswith("enable lingering failed:") and a.endswith("(units still written)") for a in actions), (
        f"no warning action in {actions}"
    )
    # The enable step still ran.
    assert ("enable", "--now", "cheaphelp.timer") in systemctl_calls


def test_install_linger_handles_missing_loginctl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing ``loginctl`` binary logs a warning and continues."""
    _patch_systemctl_success(monkeypatch)
    _patch_unit_dir(monkeypatch, tmp_path)

    def missing_loginctl(*_a: str) -> SimpleNamespace:
        msg = "loginctl not found"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(systemd, "_loginctl", missing_loginctl)
    monkeypatch.setenv("USER", "tester")

    actions = systemd.install(home=tmp_path, interval="10m", linger=True)
    assert any("enable lingering failed" in a and "(units still written)" in a for a in actions), (
        f"no warning in {actions}"
    )


# --- CLI integration -------------------------------------------------------
def test_cli_systemd_install_linger_flag_prints_action_and_suppresses_tip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--linger`` flag prints the lingering action and hides the tip."""
    from cheaphelp import main  # noqa: PLC0415

    _patch_systemctl_success(monkeypatch)
    _patch_loginctl_success(monkeypatch)
    _patch_unit_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("USER", "tester")

    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "systemd", "install", "--linger"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "enabled lingering for tester" in out
    assert "Tip:" not in out


def test_cli_systemd_install_without_linger_prints_tip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--linger`` the tip is shown and no lingering action appears."""
    from cheaphelp import main  # noqa: PLC0415

    _patch_systemctl_success(monkeypatch)
    _patch_loginctl_success(monkeypatch)
    _patch_unit_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("USER", "tester")

    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "systemd", "install"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "enabled lingering for tester" not in out
    assert "Tip:" in out


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


def test_render_units_uses_bare_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: service units must use the cheaphelp entry point, never python -m."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/usr/local/bin/cheaphelp")
    units = systemd.render_units(home=tmp_path, interval="10m")
    assert "ExecStart=/usr/local/bin/cheaphelp run" in units.service
    assert "-m cheaphelp" not in units.service
    assert "sys.executable" not in units.service
    assert "/usr/bin/python" not in units.service


def test_render_units_falls_back_to_bare_name_when_which_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``shutil.which`` can't locate cheaphelp, fall back to the bare name."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: None)
    units = systemd.render_units(home=tmp_path, interval="10m")
    assert "ExecStart=cheaphelp run" in units.service


# --- check_health -----------------------------------------------------------


def test_check_health_no_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Systemctl not on PATH -> available=False, _systemctl never called."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: None)
    mock_systemctl = MagicMock()
    monkeypatch.setattr(systemd, "_systemctl", mock_systemctl)

    result = systemd.check_health()

    assert result == systemd.Health(
        available=False,
        installed=False,
        enabled=False,
        active=False,
        last_exit_code=None,
    )
    mock_systemctl.assert_not_called()


def test_check_health_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timer installed, enabled, active, last run OK."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/bin/systemctl")
    mock_systemctl = MagicMock()
    mock_systemctl.side_effect = [
        SimpleNamespace(returncode=0, stdout="enabled", stderr=""),
        SimpleNamespace(returncode=0, stdout="active", stderr=""),
        SimpleNamespace(returncode=0, stdout="0", stderr=""),
    ]
    monkeypatch.setattr(systemd, "_systemctl", mock_systemctl)

    result = systemd.check_health()

    assert result == systemd.Health(
        available=True,
        installed=True,
        enabled=True,
        active=True,
        last_exit_code=0,
    )
    assert mock_systemctl.call_args_list == [
        call("is-enabled", "cheaphelp.timer"),
        call("is-active", "cheaphelp.timer"),
        call("show", "cheaphelp.service", "-p", "ExecMainStatus", "--value"),
    ]


def test_check_health_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """is-enabled fails -> timer not installed."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/bin/systemctl")
    mock_systemctl = MagicMock()
    mock_systemctl.side_effect = [
        SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Failed to get unit file state for cheaphelp.timer: No such file or directory",
        ),
        SimpleNamespace(returncode=3, stdout="inactive", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]
    monkeypatch.setattr(systemd, "_systemctl", mock_systemctl)

    result = systemd.check_health()

    assert result == systemd.Health(
        available=True,
        installed=False,
        enabled=False,
        active=False,
        last_exit_code=None,
    )


def test_check_health_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """is-enabled returns 'disabled' -> installed=True, enabled=False."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/bin/systemctl")
    mock_systemctl = MagicMock()
    mock_systemctl.side_effect = [
        SimpleNamespace(returncode=0, stdout="disabled", stderr=""),
        SimpleNamespace(returncode=0, stdout="inactive", stderr=""),
        SimpleNamespace(returncode=0, stdout="0", stderr=""),
    ]
    monkeypatch.setattr(systemd, "_systemctl", mock_systemctl)

    result = systemd.check_health()

    assert result == systemd.Health(
        available=True,
        installed=True,
        enabled=False,
        active=False,
        last_exit_code=0,
    )


def test_check_health_last_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ExecMainStatus != 0 -> last_exit_code reports the failure."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/bin/systemctl")
    mock_systemctl = MagicMock()
    mock_systemctl.side_effect = [
        SimpleNamespace(returncode=0, stdout="enabled", stderr=""),
        SimpleNamespace(returncode=0, stdout="active", stderr=""),
        SimpleNamespace(returncode=0, stdout="1", stderr=""),
    ]
    monkeypatch.setattr(systemd, "_systemctl", mock_systemctl)

    result = systemd.check_health()

    assert result == systemd.Health(
        available=True,
        installed=True,
        enabled=True,
        active=True,
        last_exit_code=1,
    )


def test_check_health_subprocess_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """_systemctl raises OSError -> caught, conservative fallback returned."""
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/bin/systemctl")
    mock_systemctl = MagicMock(side_effect=OSError("boom"))
    monkeypatch.setattr(systemd, "_systemctl", mock_systemctl)

    result = systemd.check_health()  # should NOT raise

    assert result == systemd.Health(
        available=True,
        installed=False,
        enabled=False,
        active=False,
        last_exit_code=None,
    )
