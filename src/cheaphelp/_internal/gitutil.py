"""Minimal git helpers for maintaining local clones of registered repos.

The responder reads a clone of the target repository so its questions and
decisions are informed by the actual codebase. Clones live under
`<workspace>/clones/` and are kept shallow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cheaphelp._internal.registry import RepoEntry


class GitError(RuntimeError):
    """Raised when a git command fails."""


def _run(args: list[str], *, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _authenticated_url(owner: str, name: str, token: str | None) -> str:
    if token:
        return f"https://x-access-token:{token}@github.com/{owner}/{name}.git"
    return f"https://github.com/{owner}/{name}.git"


def ensure_clone(clone_dir: Path, repo: RepoEntry, *, token: str | None) -> Path:
    """Ensure a fresh shallow clone of `repo` exists at `clone_dir`.

    If the clone already exists, fetch and hard-reset it to the remote default
    branch so the responder always reads current code. The authenticated URL
    (with the token) is never stored in git config or printed.
    """
    url = _authenticated_url(repo.owner, repo.name, token)
    branch = repo.default_branch or "main"

    if (clone_dir / ".git").exists():
        _run(["fetch", "--depth", "1", url, branch], cwd=clone_dir)
        _run(["checkout", "-B", branch, "FETCH_HEAD"], cwd=clone_dir)
        return clone_dir

    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(["clone", "--depth", "1", "--branch", branch, url, str(clone_dir)])
    # Scrub the tokenised remote URL so the secret is not persisted on disk.
    _run(["remote", "set-url", "origin", f"https://github.com/{repo.owner}/{repo.name}.git"], cwd=clone_dir)
    return clone_dir
