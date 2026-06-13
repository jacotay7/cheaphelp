"""Tests for the CLI."""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cheaphelp import main
from cheaphelp._internal import commands, debug
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.lock import RunLock
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


# --- daily log file --------------------------------------------------------
def _setup_workspace(tmp_path: Path) -> Workspace:
    """Build a fresh, initialised workspace under ``tmp_path``."""
    ws = Workspace(tmp_path)
    ws.ensure()
    ws.save_config(Config())  # make Workspace.exists() return True
    return ws


def test_run_writes_daily_log_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cheaphelp run` writes a per-day log file mirroring every console line."""
    ws = _setup_workspace(tmp_path)

    def fake_tick(workspace: Workspace, *, dry_run: bool, log: Callable[[str], None]) -> SimpleNamespace:  # noqa: ARG001
        log("hello-from-stub")
        log("second line")
        return SimpleNamespace(error=None, total_turns=0, repos=[])

    monkeypatch.setattr(commands, "tick", fake_tick)

    rc = main(["--home", str(ws.home), "run"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "--- tick start ---" in captured
    assert "hello-from-stub" in captured
    assert "second line" in captured
    assert "Done." in captured

    expected = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    assert expected.exists()
    assert expected.is_file()
    contents = expected.read_text(encoding="utf-8")
    lines = contents.splitlines()

    # First line is a timestamped header in the expected format.
    assert lines, "log file should not be empty"
    header_re = re.compile(
        r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] --- tick start ---\s*$",
    )
    assert header_re.match(lines[0]), f"unexpected header line: {lines[0]!r}"

    # Sentinel lines appear in the expected order.
    assert "hello-from-stub" in contents
    assert "second line" in contents
    assert contents.index("hello-from-stub") < contents.index("second line")

    # Every non-empty line in the file also appears in stdout.
    for line in lines:
        if line:
            assert line in captured, f"file line not echoed to stdout: {line!r}"


def test_run_appends_within_same_day(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running twice on the same day appends a second header + body to the daily log file."""
    ws = _setup_workspace(tmp_path)

    def fake_tick(workspace: Workspace, *, dry_run: bool, log: Callable[[str], None]) -> SimpleNamespace:  # noqa: ARG001
        log("hello-from-stub")
        return SimpleNamespace(error=None, total_turns=0, repos=[])

    monkeypatch.setattr(commands, "tick", fake_tick)

    rc1 = main(["--home", str(ws.home), "run"])
    assert rc1 == 0
    capsys.readouterr()  # discard first invocation's stdout

    log_path = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    assert log_path.exists()
    first_size = log_path.stat().st_size

    rc2 = main(["--home", str(ws.home), "run"])
    assert rc2 == 0

    second_size = log_path.stat().st_size
    assert second_size > first_size, "log file should grow when run again on the same day"

    contents = log_path.read_text(encoding="utf-8")
    assert contents.count("--- tick start ---") == 2
    assert contents.count("hello-from-stub") >= 2


def test_run_swallows_log_write_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log file that can't be written (e.g. path blocked by a directory) must not crash the tick."""
    ws = _setup_workspace(tmp_path)
    blocker = ws.logs_dir / f"run-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.log"
    blocker.mkdir()  # opening this path for writing raises IsADirectoryError (an OSError)
    try:

        def fake_tick(workspace: Workspace, *, dry_run: bool, log: Callable[[str], None]) -> SimpleNamespace:  # noqa: ARG001
            log("would-be-logged")
            return SimpleNamespace(error=None, total_turns=0, repos=[])

        monkeypatch.setattr(commands, "tick", fake_tick)

        rc = main(["--home", str(ws.home), "run"])
        assert rc == 0, "tick must not crash on a log write failure"

        captured = capsys.readouterr().out
        assert "--- tick start ---" in captured
        assert "would-be-logged" in captured
    finally:
        blocker.rmdir()


# --- run-lock skip behaviour -----------------------------------------------
def test_cmd_run_skips_when_lock_held(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When another process holds the run lock, `cheaphelp run` logs a skip message.

    The second invocation must exit 0 without doing any work, with the skip
    notice mirrored to stdout (and the daily log file).
    """
    ws = _setup_workspace(tmp_path)  # existing helper: ensure + save_config
    with RunLock(ws.run_lock_path) as holder:
        assert holder.acquired
        rc = main(["--home", str(ws.home), "run"])
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # The skip message is routed through cmd_run's `log` callback (which
    # mirrors to stdout) AND the daily log file.
    assert "already running" in combined.lower()
    assert "skipping" in combined.lower()
    # The lock is the very first thing tick() does, so it should not have
    # called out to GitHub — no "acting as @" line should appear.
    assert "acting as" not in combined
    # The "Done." summary is suppressed for a skipped tick.
    assert "Done." not in combined
