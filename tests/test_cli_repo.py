"""Tests for cheaphelp's `repo` subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cheaphelp import main
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.registry import Registry, RepoEntry
from tests.conftest import _setup_workspace


def test_repo_list_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`cheaphelp repo list --json` emits a JSON array with the required fields."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())  # make Workspace.exists() return True
    reg = Registry(ws.registry_path)
    reg.add(
        RepoEntry(owner="octocat", name="hello", default_branch="main", enabled=True),
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


# --- repo set --------------------------------------------------------------
def test_repo_set_updates_only_checks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --checks` updates only the checks field; autofix is preserved."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="old-checks",
            autofix="original-autofix",
        ),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "new-cmd",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Updated octocat/hello." in out

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "new-cmd"
    assert reloaded.autofix == "original-autofix"


def test_repo_set_updates_only_autofix(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --autofix` updates only the autofix field; checks is preserved."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="original-checks",
            autofix="old-autofix",
        ),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--autofix",
            "new-fixer",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Updated octocat/hello." in out

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "original-checks"
    assert reloaded.autofix == "new-fixer"


def test_repo_set_empty_clears_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --checks ""` clears the checks field; autofix is preserved."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="something",
            autofix="autofixer",
        ),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Updated octocat/hello." in out

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == ""
    assert reloaded.autofix == "autofixer"


def test_repo_set_unknown_repo_exits_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set` on a slug that isn't registered exits 1 and creates no file."""
    ws = _setup_workspace(tmp_path)
    # No registry file should exist yet — the failed set must not create it.
    assert not ws.registry_path.exists()

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "unknown/thing",
            "--checks",
            "x",
        ],
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown/thing is not registered." in err
    assert not ws.registry_path.exists()


def test_repo_set_no_flags_is_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set` with no flags is a no-op; the registry file is byte-identical."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="same-checks",
            autofix="same-autofix",
        ),
    )
    before_bytes = ws.registry_path.read_bytes()
    before_mtime_ns = ws.registry_path.stat().st_mtime_ns

    rc = main(["--home", str(ws.home), "repo", "set", "octocat/hello"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to update" in out

    after_bytes = ws.registry_path.read_bytes()
    after_mtime_ns = ws.registry_path.stat().st_mtime_ns
    # Byte-identical (and mtime untouched): a no-op must not touch the registry.
    assert before_bytes == after_bytes
    assert before_mtime_ns == after_mtime_ns

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "same-checks"
    assert reloaded.autofix == "same-autofix"


def test_repo_set_same_value_is_no_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --checks <same value>` reports "No changes" and leaves the registry file untouched."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="existing-cmd",
            autofix="existing-fix",
        ),
    )
    before_bytes = ws.registry_path.read_bytes()
    before_mtime_ns = ws.registry_path.stat().st_mtime_ns

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "existing-cmd",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "No changes for octocat/hello." in out
    # The old "Updated ..." message must not leak through for a no-change run.
    assert "Updated" not in out

    after_bytes = ws.registry_path.read_bytes()
    after_mtime_ns = ws.registry_path.stat().st_mtime_ns
    # Byte-identical (and mtime untouched): a no-op must not rewrite the registry.
    assert before_bytes == after_bytes
    assert before_mtime_ns == after_mtime_ns

    reloaded = Registry(ws.registry_path).find("octocat", "hello")
    assert reloaded is not None
    assert reloaded.checks == "existing-cmd"
    assert reloaded.autofix == "existing-fix"


def test_repo_set_list_reflects_update(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """After `repo set --checks`, `repo list --json` reports the new value."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(owner="octocat", name="hello", checks="old"),
    )

    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--checks",
            "new",
        ],
    )
    assert rc == 0
    capsys.readouterr()  # discard set stdout

    rc = main(["--home", str(ws.home), "repo", "list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    by_slug = {r["slug"]: r for r in data}
    assert by_slug["octocat/hello"]["checks"] == "new"


# --- blast-radius CLI flags ------------------------------------------------
def test_repo_add_stores_max_diff_files_and_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo add --max-diff-files 50 --max-diff-lines 2000` stores both fields."""
    ws = _setup_workspace(tmp_path)
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "add",
            "octocat/hello",
            "--max-diff-files",
            "50",
            "--max-diff-lines",
            "2000",
        ],
    )
    assert rc == 0
    capsys.readouterr()  # discard output

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 50
    assert entry.max_diff_lines == 2000


def test_repo_add_default_limits_when_unspecified(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo add` without flags uses the RepoEntry defaults (30, 1000)."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "repo", "add", "octocat/hello"])
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 30
    assert entry.max_diff_lines == 1000


def test_repo_update_updates_max_diff_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo update --max-diff-files 100` changes only that field."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="ruff check",
            autofix="ruff format",
            max_diff_files=30,
            max_diff_lines=1000,
        ),
    )
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "update",
            "octocat/hello",
            "--max-diff-files",
            "100",
        ],
    )
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 100
    assert entry.max_diff_lines == 1000
    assert entry.checks == "ruff check"
    assert entry.autofix == "ruff format"


def test_repo_update_updates_max_diff_lines_and_preserves_others(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo update --max-diff-lines 0` sets unlimited, preserves other fields."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="ruff check",
            autofix="ruff format",
            max_diff_files=30,
            max_diff_lines=1000,
        ),
    )
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "update",
            "octocat/hello",
            "--max-diff-lines",
            "0",
        ],
    )
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_lines == 0
    assert entry.max_diff_files == 30
    assert entry.checks == "ruff check"
    assert entry.autofix == "ruff format"


def test_repo_set_with_new_flags_still_works(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`repo set --max-diff-files 75` forwards the flag correctly."""
    ws = _setup_workspace(tmp_path)
    Registry(ws.registry_path).add(
        RepoEntry(
            owner="octocat",
            name="hello",
            checks="old-checks",
            autofix="old-autofix",
        ),
    )
    rc = main(
        [
            "--home",
            str(ws.home),
            "repo",
            "set",
            "octocat/hello",
            "--max-diff-files",
            "75",
        ],
    )
    assert rc == 0
    capsys.readouterr()

    entry = Registry(ws.registry_path).find("octocat", "hello")
    assert entry is not None
    assert entry.max_diff_files == 75
    assert entry.max_diff_lines == 1000
    assert entry.checks == "old-checks"
    assert entry.autofix == "old-autofix"
