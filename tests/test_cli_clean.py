"""Tests for cheaphelp's `clean` subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp import main
from cheaphelp._internal import commands
from cheaphelp._internal.github import Issue
from cheaphelp._internal.registry import Registry, RepoEntry
from tests.conftest import _TEST_TOKEN, _FakeGH, _seed_workspace_env, _setup_workspace


# --- clean -----------------------------------------------------------------
def test_cmd_clean_removes_closed_and_orphan_clones(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`clean` removes build clones for closed issues and unregistered repos."""
    ws = _setup_workspace(tmp_path)
    _seed_workspace_env(ws, token=_TEST_TOKEN)
    Registry(ws.registry_path).add(RepoEntry(owner="octocat", name="hello", enabled=True))

    for name in ("octocat__hello", "octocat__hello__issue-1", "octocat__hello__issue-2", "ghost__repo"):
        (ws.clones_dir / name / ".git").mkdir(parents=True)

    fake = _FakeGH("test-token")
    fake.issues["octocat/hello"] = [
        Issue(number=2, title="t", body="", state="open", labels=[], user="a", html_url=""),
    ]

    def _factory(token: str, **_kwargs: object) -> _FakeGH:
        fake.token = token
        return fake

    monkeypatch.setattr(commands, "GitHubClient", _factory)

    rc = main(["--home", str(ws.home), "clean"])
    assert rc == 0
    assert not (ws.clones_dir / "octocat__hello__issue-1").exists()  # closed -> removed
    assert (ws.clones_dir / "octocat__hello__issue-2").exists()  # open -> kept
    assert (ws.clones_dir / "octocat__hello").exists()  # shared -> kept
    assert not (ws.clones_dir / "ghost__repo").exists()  # unregistered -> removed
    assert "Removed" in capsys.readouterr().out
