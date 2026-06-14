"""Minimal git helpers for maintaining local clones of registered repos.

Two clone shapes:

- The **read-only clone** (`ensure_clone`) is shallow and hard-reset to the
  remote default branch every tick, so the responder and planner always read
  current code.
- The **work clone** (`ensure_work_clone`) is a full clone on a persistent
  per-issue branch that workers commit to across ticks; it is never reset.

The authenticated URL (with the token) is used only for individual fetch/clone/
push commands and is never stored in git config.
"""

from __future__ import annotations

import re
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


# --- build-stage helpers ---------------------------------------------------
COMMIT_AUTHOR_NAME = "cheaphelp[bot]"
COMMIT_AUTHOR_EMAIL = "cheaphelp@users.noreply.github.com"


def ensure_work_clone(clone_dir: Path, repo: RepoEntry, *, token: str | None, branch: str) -> Path:
    """Ensure a full clone exists at `clone_dir`, checked out on `branch`.

    Created from the remote default branch the first time. Existing work on the
    branch is preserved across ticks (no reset). The remote is stored without the
    token; fetch/push use an explicit authenticated URL.
    """
    base = repo.default_branch or "main"
    clean_url = f"https://github.com/{repo.owner}/{repo.name}.git"

    if not (clone_dir / ".git").exists():
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["clone", _authenticated_url(repo.owner, repo.name, token), str(clone_dir)])
        _run(["remote", "set-url", "origin", clean_url], cwd=clone_dir)

    if _run(["branch", "--list", branch], cwd=clone_dir).strip():
        _run(["checkout", branch], cwd=clone_dir)
    else:
        _run(["checkout", "-B", branch, f"origin/{base}"], cwd=clone_dir)
    return clone_dir


def commit_all(clone_dir: Path, *, message: str) -> bool:
    """Stage everything and commit. Returns False if there was nothing to commit."""
    _run(["add", "-A"], cwd=clone_dir)
    if not _run(["status", "--porcelain"], cwd=clone_dir).strip():
        return False
    _run(
        [
            "-c",
            f"user.name={COMMIT_AUTHOR_NAME}",
            "-c",
            f"user.email={COMMIT_AUTHOR_EMAIL}",
            "commit",
            "-m",
            message,
        ],
        cwd=clone_dir,
    )
    return True


def push_branch(clone_dir: Path, repo: RepoEntry, *, branch: str, token: str | None) -> None:
    """Push the current HEAD to `branch` on origin using an ephemeral auth URL."""
    url = _authenticated_url(repo.owner, repo.name, token)
    _run(["push", url, f"HEAD:refs/heads/{branch}"], cwd=clone_dir)


def run_command(clone_dir: Path, command: str, *, timeout: float = 900.0) -> tuple[int, str]:
    """Run a shell `command` in the clone; return (returncode, combined output).

    Used for the quality gate. The command comes from trusted repo config, not
    from an agent.
    """
    proc = subprocess.run(  # noqa: S602 - command is operator-configured, not agent input
        command,
        cwd=str(clone_dir),
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def diff_against_base(clone_dir: Path, repo: RepoEntry) -> tuple[str, str]:
    """Return (name-status, full unified diff) of the branch vs the base branch."""
    base = repo.default_branch or "main"
    ref = f"origin/{base}...HEAD"
    name_status = _run(["diff", "--name-status", ref], cwd=clone_dir)
    full = _run(["diff", ref], cwd=clone_dir)
    return name_status, full


# Matches the summary line of `git diff --stat` output, e.g.:
#   "1 file changed, 1 insertion(+)"
#   "2 files changed, 3 insertions(+), 1 deletion(-)"
#   "3 files changed, 45 deletions(-)"
_DIFF_STAT_RE = re.compile(
    r"(?P<files>\d+)\s+files?\s+changed"
    r"(?:,\s+(?P<insertions>\d+)\s+insertions?\(\+\))?"
    r"(?:,\s+(?P<deletions>\d+)\s+deletions?\(-\))?",
)


def parse_diff_stat(output: str) -> tuple[int, int, int] | None:
    """Parse the summary line of ``git diff --stat`` output.

    Returns ``(files, insertions, deletions)`` on a match; ``None`` when the
    output has no recognisable summary line. Missing ``insertions`` /
    ``deletions`` segments are treated as 0 (e.g. pure-additions or
    pure-deletions diffs).
    """
    for line in output.splitlines():
        match = _DIFF_STAT_RE.search(line)
        if match:
            return (
                int(match.group("files")),
                int(match.group("insertions") or 0),
                int(match.group("deletions") or 0),
            )
    return None


def diff_stat(clone_dir: Path, repo: RepoEntry) -> tuple[int, int, int] | None:
    """Return ``(files, insertions, deletions)`` of the branch vs the base.

    Uses the same 3-dot reference as :func:`diff_against_base` so the diff is
    measured against the merge base. Returns ``None`` if ``git`` errors (e.g.
    the branch has no commits beyond the base) or the output is unparseable —
    the caller treats that as 'within limits' for safety.
    """
    base = repo.default_branch or "main"
    ref = f"origin/{base}...HEAD"
    try:
        output = _run(["diff", "--stat", ref], cwd=clone_dir)
    except GitError:
        return None
    return parse_diff_stat(output)
