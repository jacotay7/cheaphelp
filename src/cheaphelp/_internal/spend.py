"""Aggregate per-day USD spend across all issues for the budget guardrail.

Persists to ``<workspace.state_dir>/daily_spend.json`` keyed by the current
UTC date, and resets when the UTC day rolls over. Read/written by the
orchestrator between agent calls.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cheaphelp._internal.opencode import UsageData

__all__ = ["DailySpendTracker"]


@dataclass
class _DayState:
    """On-disk shape for the daily spend file."""

    date: str  # YYYY-MM-DD UTC
    total_usd: float = 0.0
    warned_at: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> _DayState:
        return cls(
            date=str(data.get("date", "")),
            total_usd=float(data.get("total_usd", 0.0)),
            warned_at=list(data.get("warned_at", [])),
        )


class DailySpendTracker:
    """Aggregate USD cost for the current UTC calendar day.

    The orchestrator instantiates one of these per tick and queries
    ``daily_spend()`` before each stage dispatch to decide whether to
    proceed. ``record()`` is called from ``_record_cost`` after every
    successful agent run.
    """

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "daily_spend.json"
        self._state = self._load_or_init()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load_or_init(self) -> _DayState:
        if not self.path.exists():
            return _DayState(date=self._today())
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _DayState(date=self._today())
        if not isinstance(data, dict):
            return _DayState(date=self._today())
        return _DayState.from_dict(data)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(self._state), indent=2) + "\n",
            encoding="utf-8",
        )

    def _rollover_if_needed(self) -> None:
        today = self._today()
        if self._state.date != today:
            self._state = _DayState(date=today)
            self._save()

    def daily_spend(self) -> float:
        """Return the cumulative USD spend for the current UTC day."""
        self._rollover_if_needed()
        return self._state.total_usd

    def record(self, usage: UsageData | None) -> float:
        """Record a usage event and return the updated cumulative spend.

        If *usage* is ``None`` this is a no-op (returns current spend).
        """
        if usage is None:
            return self.daily_spend()
        self._rollover_if_needed()
        self._state.total_usd += float(usage.cost_usd)
        self._save()
        return self._state.total_usd

    def was_warned(self, fraction: float) -> bool:
        """Return ``True`` if a warning has already been issued for *fraction*."""
        return float(fraction) in self._state.warned_at

    def mark_warned(self, fraction: float) -> None:
        """Record that a warning was issued for *fraction* (idempotent)."""
        frac = float(fraction)
        if frac not in self._state.warned_at:
            self._state.warned_at.append(frac)
            self._save()
