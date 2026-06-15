"""Tests for cheaphelp's systemd unit/timer generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

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


def test_render_units_uses_bare_entry_point(tmp_path: Path) -> None:
    """Regression: service units must use the bare ``cheaphelp`` entry point,
    never a ``python -m cheaphelp`` fallback.
    """
    units = systemd.render_units(home=tmp_path, interval="10m")
    assert "ExecStart=cheaphelp run" in units.service
    assert "-m cheaphelp" not in units.service
    assert "sys.executable" not in units.service
    assert "/usr/bin/python" not in units.service


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
