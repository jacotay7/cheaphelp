"""Registry of GitHub repositories cheaphelp is allowed to act on.

Stored as `<workspace>/repos.json`. Each entry records the owner/name, the
default branch (cached for convenience) and whether the repo is currently
enabled for processing.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


def parse_slug(slug: str) -> tuple[str, str]:
    """Parse an ``owner/name`` slug, accepting full GitHub URLs too."""
    text = slug.strip()
    text = re.sub(r"^https?://github\.com/", "", text)
    text = re.sub(r"\.git$", "", text)
    text = text.strip("/")
    match = _SLUG_RE.match(text)
    if not match:
        raise ValueError(f"Invalid repository slug: {slug!r} (expected 'owner/name')")
    return match.group(1), match.group(2)


@dataclass
class RepoEntry:
    """A single registered repository."""

    owner: str
    name: str
    default_branch: str = "main"
    enabled: bool = True
    added_at: str = ""
    # Shell command run in the work clone to auto-fix trivial issues (formatting,
    # import sorting, lint --fix) before the quality gate runs. Any resulting
    # changes are committed automatically. Empty disables auto-fix.
    autofix: str = ""
    # Shell command run in the work clone as a quality gate before a PR is
    # opened (e.g. "ruff check . && pytest"). Empty disables the gate.
    checks: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


class Registry:
    """Load/save the list of registered repositories."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[RepoEntry]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [RepoEntry(**item) for item in data.get("repos", [])]

    def save(self, repos: list[RepoEntry]) -> None:
        payload = {"repos": [asdict(repo) for repo in repos]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def find(self, owner: str, name: str) -> RepoEntry | None:
        for repo in self.load():
            if repo.owner == owner and repo.name == name:
                return repo
        return None

    def add(self, entry: RepoEntry) -> bool:
        """Add a repo. Returns False if it was already registered."""
        repos = self.load()
        for existing in repos:
            if existing.owner == entry.owner and existing.name == entry.name:
                return False
        if not entry.added_at:
            entry.added_at = datetime.now(timezone.utc).isoformat()
        repos.append(entry)
        self.save(repos)
        return True

    def remove(self, owner: str, name: str) -> bool:
        """Remove a repo. Returns False if it was not registered."""
        repos = self.load()
        kept = [r for r in repos if not (r.owner == owner and r.name == name)]
        if len(kept) == len(repos):
            return False
        self.save(kept)
        return True

    def set_enabled(self, owner: str, name: str, enabled: bool) -> bool:
        repos = self.load()
        changed = False
        for repo in repos:
            if repo.owner == owner and repo.name == name:
                repo.enabled = enabled
                changed = True
        if changed:
            self.save(repos)
        return changed

    def update(self, owner: str, name: str, **fields: Any) -> bool:
        """Update mutable fields on a registered repo in place.

        Only fields that exist on ``RepoEntry`` and are explicitly set to a
        non-``None`` value are applied. Unknown kwargs are ignored. Returns
        ``True`` only when the repo is registered and at least one supplied
        field actually changes (in which case the file is rewritten).
        Returns ``False`` when the repo is not registered, or when the repo
        is registered but every supplied field already matches its current
        value on disk (in which case the file is not rewritten). Callers
        that need to distinguish "not found" from "no change" should use
        ``find()`` first to check existence.
        """
        repos = self.load()
        target: RepoEntry | None = None
        for repo in repos:
            if repo.owner == owner and repo.name == name:
                target = repo
                break
        if target is None:
            return False
        changed = False
        for key, value in fields.items():
            if value is None:
                continue
            if not hasattr(target, key):
                continue  # unknown field; ignore silently
            if getattr(target, key) != value:
                setattr(target, key, value)
                changed = True
        if changed:
            self.save(repos)
        return changed
