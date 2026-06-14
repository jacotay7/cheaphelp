"""Tests for cheaphelp's build-clone cleanup helpers."""

from __future__ import annotations

from pathlib import Path

from cheaphelp._internal import (
    cleanup,
)
from cheaphelp._internal.config import Workspace
from cheaphelp._internal.lock import RunLock
from cheaphelp._internal.registry import RepoEntry


def _make_clone(ws: Workspace, name: str) -> Path:
    """Create a fake clone directory (with a .git marker) under the workspace."""
    path = ws.clones_dir / name
    (path / ".git").mkdir(parents=True)
    return path


def test_iter_issue_work_clones(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    _make_clone(ws, "octocat__hello")  # shared clone: excluded
    _make_clone(ws, "octocat__hello__issue-1")
    _make_clone(ws, "octocat__hello__issue-12")
    _make_clone(ws, "octocat__other__issue-3")  # different repo: excluded
    assert set(cleanup.iter_issue_work_clones(ws, "octocat", "hello")) == {1, 12}


def test_prune_repo_work_clones_removes_closed_keeps_live(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    for n in (1, 2, 3):
        _make_clone(ws, f"octocat__hello__issue-{n}")

    removed = cleanup.prune_repo_work_clones(ws, repo, {2})  # only #2 is still open
    assert sorted(removed) == [1, 3]
    assert not (ws.clones_dir / "octocat__hello__issue-1").exists()
    assert (ws.clones_dir / "octocat__hello__issue-2").exists()
    assert not (ws.clones_dir / "octocat__hello__issue-3").exists()


def test_prune_repo_work_clones_dry_run_keeps_files(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    _make_clone(ws, "octocat__hello__issue-9")
    removed = cleanup.prune_repo_work_clones(ws, repo, set(), dry_run=True)
    assert removed == [9]
    assert (ws.clones_dir / "octocat__hello__issue-9").exists()  # reported, not deleted


def test_prune_repo_work_clones_skips_locked(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="octocat", name="hello")
    _make_clone(ws, "octocat__hello__issue-5")
    # A concurrent tick holds the issue lock -> pruning leaves it alone.
    with RunLock(ws.issue_lock_path("octocat", "hello", 5)) as held:
        assert held.acquired
        removed = cleanup.prune_repo_work_clones(ws, repo, set())
    assert removed == []
    assert (ws.clones_dir / "octocat__hello__issue-5").exists()


def test_prune_orphan_clones(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    _make_clone(ws, "octocat__hello")
    _make_clone(ws, "octocat__hello__issue-1")
    _make_clone(ws, "ghost__repo")
    _make_clone(ws, "ghost__repo__issue-2")
    removed = cleanup.prune_orphan_clones(ws, [RepoEntry(owner="octocat", name="hello")])
    assert set(removed) == {"ghost__repo", "ghost__repo__issue-2"}
    assert (ws.clones_dir / "octocat__hello").exists()  # registered: kept
    assert (ws.clones_dir / "octocat__hello__issue-1").exists()
