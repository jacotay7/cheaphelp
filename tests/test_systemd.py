"""Tests for cheaphelp's systemd unit/timer generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_install_linger_calls_loginctl_with_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``install(linger=True)`` calls ``loginctl enable-linger <user>``."""
    _patch_systemctl_success(monkeypatch)
    _patch_loginctl_success(monkeypatch)
    monkeypatch.setenv("USER", "tester")

    actions = systemd.install(home=tmp_path, interval="10m", linger=True)
    assert "enabled lingering for tester" in actions


def test_install_linger_default_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``install(linger=False)`` does not call ``loginctl``."""
    _patch_systemctl_success(monkeypatch)

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
    monkeypatch.setenv("USER", "tester")

    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "systemd", "install"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "enabled lingering for tester" not in out
    assert "Tip:" in out
