"""Tests for cheaphelp's ``doctor`` subcommand — systemd health integration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cheaphelp import main
from cheaphelp._internal import commands, systemd
from cheaphelp._internal.env import GITHUB_TOKEN_KEY, OPENROUTER_API_KEY, update_env_file
from cheaphelp._internal.registry import Registry, RepoEntry
from tests.conftest import _FakeGH, _setup_workspace


def _gh_factory(token: str, **kwargs: object) -> _FakeGH:
    """Factory that returns a _FakeGH instance for mocking GitHubClient."""
    return _FakeGH(token, **kwargs)


# --- no systemctl ------------------------------------------------------------


def test_doctor_no_systemctl(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When systemd is not available (no systemctl), no systemd line appears and rc == 0."""
    ws = _setup_workspace(tmp_path)
    update_env_file(ws.env_path, {GITHUB_TOKEN_KEY: "t", OPENROUTER_API_KEY: "k"})
    Registry(ws.registry_path).add(
        RepoEntry(owner="o", name="r", default_branch="main", enabled=True),
    )

    monkeypatch.setattr(
        commands.systemd,
        "check_health",
        lambda: systemd.Health(
            available=False,
            installed=False,
            enabled=False,
            active=False,
            last_exit_code=None,
        ),
    )
    monkeypatch.setattr(commands, "GitHubClient", _gh_factory)
    monkeypatch.setattr(commands.opencode, "find_opencode", lambda *a, **kw: "/fake/opencode")

    rc = main(["--home", str(ws.home), "doctor"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "systemd timer" not in captured


# --- timer not installed ----------------------------------------------------


def test_doctor_timer_not_installed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When systemctl is available but the timer is not installed, a [--] line is shown and rc == 0."""
    ws = _setup_workspace(tmp_path)
    update_env_file(ws.env_path, {GITHUB_TOKEN_KEY: "t", OPENROUTER_API_KEY: "k"})
    Registry(ws.registry_path).add(
        RepoEntry(owner="o", name="r", default_branch="main", enabled=True),
    )

    monkeypatch.setattr(
        commands.systemd,
        "check_health",
        lambda: systemd.Health(
            available=True,
            installed=False,
            enabled=False,
            active=False,
            last_exit_code=None,
        ),
    )
    monkeypatch.setattr(commands, "GitHubClient", _gh_factory)
    monkeypatch.setattr(commands.opencode, "find_opencode", lambda *a, **kw: "/fake/opencode")

    rc = main(["--home", str(ws.home), "doctor"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert re.search(r"\[--\s*\]\s+systemd timer: not installed", captured)


# --- timer healthy ----------------------------------------------------------


def test_doctor_timer_healthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the timer is installed, enabled, active, and last run succeeded, an [OK] line and rc == 0."""
    ws = _setup_workspace(tmp_path)
    update_env_file(ws.env_path, {GITHUB_TOKEN_KEY: "t", OPENROUTER_API_KEY: "k"})
    Registry(ws.registry_path).add(
        RepoEntry(owner="o", name="r", default_branch="main", enabled=True),
    )

    monkeypatch.setattr(
        commands.systemd,
        "check_health",
        lambda: systemd.Health(
            available=True,
            installed=True,
            enabled=True,
            active=True,
            last_exit_code=0,
        ),
    )
    monkeypatch.setattr(commands, "GitHubClient", _gh_factory)
    monkeypatch.setattr(commands.opencode, "find_opencode", lambda *a, **kw: "/fake/opencode")

    rc = main(["--home", str(ws.home), "doctor"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert re.search(r"\[OK\s+\]\s+systemd timer: cheaphelp\.timer \(enabled, active\)", captured)


# --- timer disabled ---------------------------------------------------------


def test_doctor_timer_disabled(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the timer is installed but disabled, a [FAIL] line is shown and rc == 1."""
    ws = _setup_workspace(tmp_path)
    update_env_file(ws.env_path, {GITHUB_TOKEN_KEY: "t", OPENROUTER_API_KEY: "k"})
    Registry(ws.registry_path).add(
        RepoEntry(owner="o", name="r", default_branch="main", enabled=True),
    )

    monkeypatch.setattr(
        commands.systemd,
        "check_health",
        lambda: systemd.Health(
            available=True,
            installed=True,
            enabled=False,
            active=False,
            last_exit_code=0,
        ),
    )
    monkeypatch.setattr(commands, "GitHubClient", _gh_factory)
    monkeypatch.setattr(commands.opencode, "find_opencode", lambda *a, **kw: "/fake/opencode")

    rc = main(["--home", str(ws.home), "doctor"])
    assert rc == 1

    captured = capsys.readouterr().out
    assert re.search(r"\[FAIL\]\s+systemd timer:.*\(not enabled\)", captured)


# --- last run failed --------------------------------------------------------


def test_doctor_last_run_failed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the timer is healthy but the last service run exited non-zero, a [FAIL] line and rc == 1."""
    ws = _setup_workspace(tmp_path)
    update_env_file(ws.env_path, {GITHUB_TOKEN_KEY: "t", OPENROUTER_API_KEY: "k"})
    Registry(ws.registry_path).add(
        RepoEntry(owner="o", name="r", default_branch="main", enabled=True),
    )

    monkeypatch.setattr(
        commands.systemd,
        "check_health",
        lambda: systemd.Health(
            available=True,
            installed=True,
            enabled=True,
            active=True,
            last_exit_code=2,
        ),
    )
    monkeypatch.setattr(commands, "GitHubClient", _gh_factory)
    monkeypatch.setattr(commands.opencode, "find_opencode", lambda *a, **kw: "/fake/opencode")

    rc = main(["--home", str(ws.home), "doctor"])
    assert rc == 1

    captured = capsys.readouterr().out
    assert re.search(r"\[FAIL\]\s+systemd timer:.*\(last run failed", captured)
