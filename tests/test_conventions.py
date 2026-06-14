"""Tests for cheaphelp's repo-conventions discovery."""

from __future__ import annotations

from pathlib import Path

from cheaphelp._internal.conventions import CONVENTIONS_FILES, read_conventions


# --- conventions ------------------------------------------------------------
def test_read_conventions_no_file(tmp_path: Path) -> None:
    assert read_conventions(tmp_path) == ""


def test_read_conventions_cheaphelp_md(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CHEAPHELP.md").write_text("hello", encoding="utf-8")
    assert read_conventions(d) == "hello"


def test_read_conventions_agents_md(tmp_path: Path) -> None:
    d = tmp_path
    (d / "AGENTS.md").write_text("world", encoding="utf-8")
    assert read_conventions(d) == "world"


def test_read_conventions_contributing_md(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CONTRIBUTING.md").write_text("contrib", encoding="utf-8")
    assert read_conventions(d) == "contrib"


def test_read_conventions_precedence_cheaphelp_wins(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CHEAPHELP.md").write_text("ch", encoding="utf-8")
    (d / "AGENTS.md").write_text("ag", encoding="utf-8")
    (d / "CONTRIBUTING.md").write_text("ct", encoding="utf-8")
    assert read_conventions(d) == "ch"


def test_read_conventions_precedence_agents_when_no_cheaphelp(tmp_path: Path) -> None:
    d = tmp_path
    (d / "AGENTS.md").write_text("ag", encoding="utf-8")
    (d / "CONTRIBUTING.md").write_text("ct", encoding="utf-8")
    assert read_conventions(d) == "ag"


def test_read_conventions_missing_dir(tmp_path: Path) -> None:
    assert read_conventions(tmp_path / "nope") == ""


def test_read_conventions_not_a_dir(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x", encoding="utf-8")
    assert read_conventions(f) == ""


def test_read_conventions_empty_file(tmp_path: Path) -> None:
    d = tmp_path
    (d / "CHEAPHELP.md").write_text("", encoding="utf-8")
    assert read_conventions(d) == ""


def test_conventions_files_constant() -> None:
    assert CONVENTIONS_FILES == ("CHEAPHELP.md", "AGENTS.md", "CONTRIBUTING.md")
