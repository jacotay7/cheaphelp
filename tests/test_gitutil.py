"""Tests for cheaphelp's git plumbing helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal import (
    gitutil,
)


# --- parse_diff_stat -------------------------------------------------------
def test_parse_diff_stat_plural_full() -> None:
    """Full stat line with multiple files, insertions and deletions."""
    result = gitutil.parse_diff_stat(
        " src/foo.py | 4 ++--\n src/bar.py | 2 +\n 2 files changed, 3 insertions(+), 3 deletions(-)",
    )
    assert result == (2, 3, 3)


def test_parse_diff_stat_singular_no_deletions() -> None:
    """Singular forms: 1 file, 1 insertion, no deletions."""
    result = gitutil.parse_diff_stat(" 1 file changed, 1 insertion(+)")
    assert result == (1, 1, 0)


def test_parse_diff_stat_singular_full() -> None:
    """Singular forms: 1 file, 1 insertion, 1 deletion."""
    result = gitutil.parse_diff_stat(" 1 file changed, 1 insertion(+), 1 deletion(-)")
    assert result == (1, 1, 1)


def test_parse_diff_stat_no_insertions() -> None:
    """Only deletions present (no insertions segment)."""
    result = gitutil.parse_diff_stat(" 3 files changed, 45 deletions(-)")
    assert result == (3, 0, 45)


def test_parse_diff_stat_empty() -> None:
    """Empty string returns None."""
    result = gitutil.parse_diff_stat("")
    assert result is None


def test_parse_diff_stat_garbage() -> None:
    """Unrecognisable prose returns None."""
    result = gitutil.parse_diff_stat("some prose, not a stat line")
    assert result is None


def test_parse_diff_stat_no_summary_line() -> None:
    """File-level diff lines with no summary line return None."""
    result = gitutil.parse_diff_stat(" src/foo.py | 4 ++--")
    assert result is None


def test_git_run_error_redacts_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing git command must not leak the auth token in its GitError."""
    from types import SimpleNamespace  # noqa: PLC0415

    def fake_run(*_a: object, **_k: object) -> object:
        return SimpleNamespace(returncode=1, stdout="", stderr="remote rejected")

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    url = "https://x-access-token:supersecret_token@github.com/o/r.git"
    with pytest.raises(gitutil.GitError) as excinfo:
        gitutil._run(["push", url, "HEAD:refs/heads/b"])
    message = str(excinfo.value)
    assert "supersecret_token" not in message
    assert "***@github.com" in message


def test_rev_parse_returns_sha(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    gitutil._run(["init"], cwd=repo)
    gitutil._run(["config", "user.email", "test@test"], cwd=repo)
    gitutil._run(["config", "user.name", "Test"], cwd=repo)
    gitutil._run(["commit", "--allow-empty", "-m", "first"], cwd=repo)
    sha = gitutil.rev_parse(repo)
    assert isinstance(sha, str)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
