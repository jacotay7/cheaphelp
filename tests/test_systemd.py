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
    assert units.service.rstrip().endswith("-m cheaphelp run")
    assert f"CHEAPHELP_HOME={tmp_path}" in units.service
