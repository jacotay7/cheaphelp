"""Tests for cheaphelp's daily spend tracker."""

from __future__ import annotations

import json
from pathlib import Path

from cheaphelp._internal import opencode
from cheaphelp._internal.spend import DailySpendTracker


def test_daily_spend_tracker_initialises_file(tmp_path: Path) -> None:
    """Fresh workspace; tracker creates file with zero spend after first record."""
    tracker = DailySpendTracker(tmp_path)
    assert tracker.daily_spend() == 0.0
    # File is created lazily on first record() call.
    assert not tracker.path.exists()
    tracker.record(opencode.UsageData(cost_usd=0.0))
    assert tracker.path.exists()
    data = json.loads(tracker.path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "date" in data
    assert data["total_usd"] == 0.0
    assert data["warned_at"] == []


def test_daily_spend_tracker_record_accumulates_and_persists(tmp_path: Path) -> None:
    """Two records; subsequent daily_spend() returns the sum; cross-instance reads match."""
    tracker = DailySpendTracker(tmp_path)
    u1 = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    u2 = opencode.UsageData(prompt_tokens=5, completion_tokens=5, total_tokens=10, cost_usd=0.0005)

    total = tracker.record(u1)
    assert total == 0.001
    total = tracker.record(u2)
    assert total == 0.0015

    # Cross-instance read.
    tracker2 = DailySpendTracker(tmp_path)
    assert tracker2.daily_spend() == 0.0015


def test_daily_spend_tracker_rolls_over_on_new_day(tmp_path: Path) -> None:
    """Mutate tracker._state.date to a past date, call daily_spend(), assert it returns 0.0."""
    tracker = DailySpendTracker(tmp_path)
    tracker.record(opencode.UsageData(cost_usd=0.5))
    assert tracker.daily_spend() == 0.5

    # Force the state to yesterday.
    tracker._state.date = "2020-01-01"
    assert tracker.daily_spend() == 0.0
    # The file should now have today's date and zero spend.
    data = json.loads(tracker.path.read_text(encoding="utf-8"))
    assert data["total_usd"] == 0.0
    assert data["date"] != "2020-01-01"


def test_daily_spend_tracker_mark_warned_idempotent(tmp_path: Path) -> None:
    """mark_warned(0.8) twice; was_warned(0.8) is True; file contains warned_at: [0.8] exactly once."""
    tracker = DailySpendTracker(tmp_path)
    tracker.mark_warned(0.8)
    tracker.mark_warned(0.8)
    assert tracker.was_warned(0.8) is True
    data = json.loads(tracker.path.read_text(encoding="utf-8"))
    assert data["warned_at"] == [0.8]


def test_daily_spend_tracker_record_none_is_noop(tmp_path: Path) -> None:
    """record(None) returns current spend without modifying state."""
    tracker = DailySpendTracker(tmp_path)
    assert tracker.record(None) == 0.0
    tracker.record(opencode.UsageData(cost_usd=0.1))
    assert tracker.record(None) == 0.1
    assert tracker.daily_spend() == 0.1


def test_daily_spend_tracker_corrupt_file_resets(tmp_path: Path) -> None:
    """Corrupt JSON in the file is treated as a fresh day."""
    path = tmp_path / "daily_spend.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    tracker = DailySpendTracker(tmp_path)
    assert tracker.daily_spend() == 0.0

    # Non-dict JSON also resets.
    path.write_text("[]", encoding="utf-8")
    tracker2 = DailySpendTracker(tmp_path)
    assert tracker2.daily_spend() == 0.0


def test_daily_spend_tracker_was_warned_false_for_unseen(tmp_path: Path) -> None:
    """was_warned returns False for a fraction that has not been warned."""
    tracker = DailySpendTracker(tmp_path)
    assert tracker.was_warned(0.8) is False
    assert tracker.was_warned(0.95) is False
