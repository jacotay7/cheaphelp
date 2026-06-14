"""Persist PR ↔ issue link state on disk for the rework stage.

Schema for ``pr_state.json`` stored in the issue's workspace directory::

    {
        "pr_number": int,
        "pr_url": str,
        "last_push_sha": str,
        "reviewers": list[str],
        "rework_attempts": int,
        "updated_at": str   // ISO-8601 UTC timestamp
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def load_pr_state(issue_dir: Path) -> dict | None:
    r"""Read *issue_dir* ``/pr_state.json`` and return the parsed dict.

    Returns ``None`` if the file does not exist or cannot be parsed (corrupt
    JSON). Never raises.
    """
    path = issue_dir / "pr_state.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_pr_state(issue_dir: Path, data: dict) -> None:
    r"""Write *data* (with an auto-added ``updated_at`` timestamp) to *issue_dir* ``/pr_state.json``.

    Creates the directory if it does not exist.
    """
    issue_dir.mkdir(parents=True, exist_ok=True)
    payload = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
    path = issue_dir / "pr_state.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
