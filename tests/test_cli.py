"""Tests for the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cheaphelp import main
from cheaphelp._internal import debug
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.registry import Registry, RepoEntry


def test_main() -> None:
    """With no subcommand the CLI prints help and returns non-zero."""
    assert main([]) == 1


def test_show_help(capsys: pytest.CaptureFixture) -> None:
    """Show help.

    Parameters:
        capsys: Pytest fixture to capture output.
    """
    with pytest.raises(SystemExit):
        main(["-h"])
    captured = capsys.readouterr()
    assert "cheaphelp" in captured.out


def test_show_version(capsys: pytest.CaptureFixture) -> None:
    """Show version.

    Parameters:
        capsys: Pytest fixture to capture output.
    """
    with pytest.raises(SystemExit):
        main(["-V"])
    captured = capsys.readouterr()
    assert debug._get_version() in captured.out


def test_show_debug_info(capsys: pytest.CaptureFixture) -> None:
    """Show debug information.

    Parameters:
        capsys: Pytest fixture to capture output.
    """
    with pytest.raises(SystemExit):
        main(["--debug-info"])
    captured = capsys.readouterr().out.lower()
    assert "python" in captured
    assert "system" in captured
    assert "environment" in captured
    assert "packages" in captured


def test_repo_list_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`cheaphelp repo list --json` emits a JSON array with the required fields."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())  # make Workspace.exists() return True
    reg = Registry(ws.registry_path)
    reg.add(
        RepoEntry(owner="octocat", name="hello", default_branch="main", enabled=True)
    )
    reg.add(RepoEntry(owner="octocat", name="bye", default_branch="dev", enabled=False))

    rc = main(["--home", str(ws.home), "repo", "list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 2
    by_slug = {r["slug"]: r for r in data}
    assert set(by_slug) == {"octocat/hello", "octocat/bye"}
    for required in ("owner", "name", "slug", "default_branch", "enabled"):
        assert required in by_slug["octocat/hello"]
    assert by_slug["octocat/hello"]["enabled"] is True
    assert by_slug["octocat/bye"]["default_branch"] == "dev"
    assert by_slug["octocat/bye"]["enabled"] is False


def test_repo_list_text_default_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Without --json, the human-readable output is preserved; with --json, an empty list is `[]`."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())  # make Workspace.exists() return True
    reg = Registry(ws.registry_path)
    reg.add(RepoEntry(owner="octocat", name="hello"))

    # Default (no flag) — human-readable line with the repo slug.
    rc = main(["--home", str(ws.home), "repo", "list"])
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "octocat/hello" in text_out
    # The text path must not leak JSON braces.
    assert '{"owner"' not in text_out

    # Empty registry + --json -> `[]` (not the "No repositories registered" text).
    reg.remove("octocat", "hello")
    rc = main(["--home", str(ws.home), "repo", "list", "--json"])
    assert rc == 0
    json_out = capsys.readouterr().out
    assert json.loads(json_out) == []
