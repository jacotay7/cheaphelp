"""Tests for cheaphelp's systemd unit/timer generation."""

from __future__ import annotations

from pathlib import Path

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
