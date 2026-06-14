"""Workspace maintenance: prune build clones the harness no longer needs.

The orchestrator keeps two kinds of clone under ``<workspace>/clones/``:

- a shared read-only clone per repo, ``<owner>__<repo>`` (reused every tick), and
- a build clone per issue, ``<owner>__<repo>__issue-<n>`` (used while the issue is
  being implemented).

Build clones are only needed while an issue is *live* (open). Once it closes they
are dead weight — and they are large (a full working tree each). This module
removes build clones for closed issues, and any clone belonging to a repo that is
no longer registered. Per-issue **state** under ``state/`` is intentionally left
untouched so it stays available for inspection and debugging.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from cheaphelp._internal.config import Workspace
from cheaphelp._internal.lock import RunLock
from cheaphelp._internal.registry import RepoEntry

Logger = Callable[[str], None]


def iter_issue_work_clones(workspace: Workspace, owner: str, name: str) -> dict[int, Path]:
    """Map issue number -> build-clone path for one repo's per-issue clones.

    The repo's shared clone (``<owner>__<repo>``, no ``__issue-`` suffix) is never
    included. Matching by the known ``<owner>__<repo>__issue-`` prefix avoids the
    ambiguity of splitting on ``__`` (repo names may contain underscores).
    """
    prefix = f"{owner}__{name}__issue-"
    clones: dict[int, Path] = {}
    if not workspace.clones_dir.exists():
        return clones
    for path in workspace.clones_dir.iterdir():
        if path.is_dir() and path.name.startswith(prefix):
            suffix = path.name[len(prefix) :]
            if suffix.isdigit():
                clones[int(suffix)] = path
    return clones


def prune_repo_work_clones(
    workspace: Workspace,
    repo: RepoEntry,
    live_numbers: set[int],
    *,
    dry_run: bool = False,
    log: Logger | None = None,
) -> list[int]:
    """Remove build clones for this repo's issues that are no longer live.

    ``live_numbers`` is the set of currently-open issue numbers; any build clone for
    an issue outside it is removed. Returns the issue numbers whose clones were
    removed (or, in dry-run, would be).
    """
    log = log or (lambda _m: None)
    removed: list[int] = []
    for number, path in sorted(iter_issue_work_clones(workspace, repo.owner, repo.name).items()):
        if number in live_numbers:
            continue
        if dry_run:
            log(f"  · would remove build clone for {repo.slug}#{number}")
            removed.append(number)
            continue
        # Guard against a concurrent tick still using this issue's clone. Closed
        # issues are never actionable, so this should never contend in practice.
        with RunLock(workspace.issue_lock_path(repo.owner, repo.name, number)) as lock:
            if not lock.acquired:
                continue
            shutil.rmtree(path, ignore_errors=True)
        log(f"  · removed build clone for {repo.slug}#{number}")
        removed.append(number)
    return removed


def prune_orphan_clones(
    workspace: Workspace,
    repos: Iterable[RepoEntry],
    *,
    dry_run: bool = False,
    log: Logger | None = None,
) -> list[str]:
    """Remove clone dirs (shared or per-issue) for repos no longer registered.

    Returns the clone directory names that were removed (or would be, in dry-run).
    """
    log = log or (lambda _m: None)
    if not workspace.clones_dir.exists():
        return []
    known = {f"{r.owner}__{r.name}" for r in repos}
    removed: list[str] = []
    for path in workspace.clones_dir.iterdir():
        if not path.is_dir():
            continue
        # A clone belongs to a registered repo if its name equals "<owner>__<repo>"
        # or starts with "<owner>__<repo>__issue-".
        if any(path.name == k or path.name.startswith(f"{k}__issue-") for k in known):
            continue
        if dry_run:
            log(f"  · would remove orphaned clone {path.name}")
        else:
            shutil.rmtree(path, ignore_errors=True)
            log(f"  · removed orphaned clone {path.name}")
        removed.append(path.name)
    return removed
