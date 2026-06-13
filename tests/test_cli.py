"""Tests for the CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from cheaphelp import main
from cheaphelp._internal import debug
from cheaphelp._internal.cli import get_parser
from cheaphelp._internal.commands import cmd_repo_list
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


# --- repo list -------------------------------------------------------------
def test_repo_list_text_default(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Default `repo list` output is human-readable, not valid JSON.

    Parameters:
        tmp_path: Pytest fixture providing a temporary directory.
        capsys: Pytest fixture to capture output.
    """
    ws = Workspace(tmp_path / "ws")
    ws.ensure()
    ws.save_config(Config())
    reg = Registry(ws.registry_path)
    reg.add(RepoEntry(owner="o1", name="r1"))
    reg.add(RepoEntry(owner="o2", name="r2", default_branch="develop", enabled=False))

    rc = cmd_repo_list(argparse.Namespace(home=str(ws.home), json=False))
    out = capsys.readouterr().out

    assert rc == 0
    assert "o1/r1" in out
    assert "o2/r2" in out
    assert "develop" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_repo_list_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`repo list --json` emits a JSON array of repository dicts.

    Parameters:
        tmp_path: Pytest fixture providing a temporary directory.
        capsys: Pytest fixture to capture output.
    """
    ws = Workspace(tmp_path / "ws")
    ws.ensure()
    ws.save_config(Config())
    reg = Registry(ws.registry_path)
    reg.add(RepoEntry(owner="o1", name="r1"))
    reg.add(RepoEntry(owner="o2", name="r2", default_branch="develop", enabled=False))

    rc = cmd_repo_list(argparse.Namespace(home=str(ws.home), json=True))
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) == 2

    by_slug = {entry["slug"]: entry for entry in payload}
    assert set(by_slug) == {"o1/r1", "o2/r2"}

    for entry in payload:
        assert set(entry) >= {"owner", "name", "slug", "default_branch", "enabled"}
        assert entry["slug"] == f"{entry['owner']}/{entry['name']}"

    assert by_slug["o1/r1"] == {
        "owner": "o1",
        "name": "r1",
        "slug": "o1/r1",
        "default_branch": "main",
        "enabled": True,
    }
    assert by_slug["o2/r2"] == {
        "owner": "o2",
        "name": "r2",
        "slug": "o2/r2",
        "default_branch": "develop",
        "enabled": False,
    }


def test_repo_list_json_empty(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`repo list --json` on an empty registry prints `[]`.

    Parameters:
        tmp_path: Pytest fixture providing a temporary directory.
        capsys: Pytest fixture to capture output.
    """
    ws = Workspace(tmp_path / "ws")
    ws.ensure()
    ws.save_config(Config())

    rc = cmd_repo_list(argparse.Namespace(home=str(ws.home), json=True))
    out = capsys.readouterr().out

    assert rc == 0
    assert out.strip() == "[]"
    assert json.loads(out) == []


def test_repo_list_missing_workspace_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`repo list --json` against a missing workspace reports on stderr, exits 1.

    Stdout must stay empty so a downstream ``json.loads`` consumer never sees
    garbage mixed in with the JSON stream.

    Parameters:
        tmp_path: Pytest fixture providing a temporary directory.
        capsys: Pytest fixture to capture output.
    """
    ws = Workspace(tmp_path / "ws")
    # Intentionally do NOT call ws.ensure().

    rc = cmd_repo_list(argparse.Namespace(home=str(ws.home), json=True))
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert "No workspace" in captured.err
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.out)


def test_repo_list_flag_parsed() -> None:
    """The `--json` flag is parsed on `repo list` and defaults to False."""
    with_json = get_parser().parse_args(["repo", "list", "--json"])
    assert with_json.json is True

    without_json = get_parser().parse_args(["repo", "list"])
    assert without_json.json is False
