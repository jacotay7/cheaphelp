"""Tests for cheaphelp's Workspace/Config, env, and registry modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal.config import DEFAULT_AGENT_TIMEOUT, DEFAULT_MODELS, Config, Workspace
from cheaphelp._internal.env import parse_env, read_env_file, update_env_file
from cheaphelp._internal.registry import Registry, RepoEntry, parse_slug


# --- config / workspace ----------------------------------------------------
def test_workspace_roundtrip(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    assert not ws.exists()
    ws.ensure()
    ws.save_config(Config())
    assert ws.exists()
    loaded = ws.load_config()
    assert loaded.models == DEFAULT_MODELS
    assert loaded.model_for("responder") == DEFAULT_MODELS["responder"]


def test_config_merges_defaults() -> None:
    cfg = Config.from_dict({"models": {"worker": "openrouter/custom"}})
    assert cfg.model_for("worker") == "openrouter/custom"
    # Missing roles fall back to defaults.
    assert cfg.model_for("responder") == DEFAULT_MODELS["responder"]


def test_agent_timeout_default_and_roundtrip() -> None:
    # Default when constructed with no args.
    assert Config().agent_timeout == DEFAULT_AGENT_TIMEOUT == 600.0
    # Default when the key is absent from the on-disk dict.
    assert Config.from_dict({}).agent_timeout == 600.0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"agent_timeout": 1200})
    assert cfg.agent_timeout == 1200.0
    assert Config.from_dict(cfg.to_dict()).agent_timeout == 1200.0
    # Float-typed values are accepted (the spec says `float`).
    assert Config.from_dict({"agent_timeout": 30.5}).agent_timeout == 30.5


def test_max_issues_per_tick_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().max_issues_per_tick == 0
    assert Config.from_dict({}).max_issues_per_tick == 0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"max_issues_per_tick": 5})
    assert cfg.max_issues_per_tick == 5
    assert Config.from_dict(cfg.to_dict()).max_issues_per_tick == 5
    # String values are coerced via int(...).
    assert Config.from_dict({"max_issues_per_tick": "7"}).max_issues_per_tick == 7


def test_max_tasks_per_tick_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().max_tasks_per_tick == 0
    assert Config.from_dict({}).max_tasks_per_tick == 0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"max_tasks_per_tick": 3})
    assert cfg.max_tasks_per_tick == 3
    assert Config.from_dict(cfg.to_dict()).max_tasks_per_tick == 3
    # String values are coerced via int(...).
    assert Config.from_dict({"max_tasks_per_tick": "2"}).max_tasks_per_tick == 2


def test_max_task_attempts_default_and_roundtrip() -> None:
    # Defaults to 2 (one retry) when unset.
    assert Config().max_task_attempts == 2
    assert Config.from_dict({}).max_task_attempts == 2
    cfg = Config.from_dict({"max_task_attempts": 4})
    assert cfg.max_task_attempts == 4
    assert Config.from_dict(cfg.to_dict()).max_task_attempts == 4


def test_prune_work_clones_default_and_roundtrip() -> None:
    assert Config().prune_work_clones is True
    assert Config.from_dict({}).prune_work_clones is True
    cfg = Config.from_dict({"prune_work_clones": False})
    assert cfg.prune_work_clones is False
    assert Config.from_dict(cfg.to_dict()).prune_work_clones is False


def test_retry_attempts_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().retry_attempts == 3
    assert Config.from_dict({}).retry_attempts == 3
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"retry_attempts": 5})
    assert cfg.retry_attempts == 5
    assert Config.from_dict(cfg.to_dict()).retry_attempts == 5
    # String values are coerced via int(...).
    assert Config.from_dict({"retry_attempts": "4"}).retry_attempts == 4


def test_retry_base_delay_default_and_roundtrip() -> None:
    # Default when constructed with no args / absent from the on-disk dict.
    assert Config().retry_base_delay == 1.0
    assert Config.from_dict({}).retry_base_delay == 1.0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"retry_base_delay": 2.5})
    assert cfg.retry_base_delay == 2.5
    assert Config.from_dict(cfg.to_dict()).retry_base_delay == 2.5
    # String values are coerced via float(...).
    assert Config.from_dict({"retry_base_delay": "0.5"}).retry_base_delay == 0.5


# --- env -------------------------------------------------------------------
def test_parse_env_handles_quotes_and_comments() -> None:
    parsed = parse_env('# comment\nexport A="hello"\nB=plain\n\nC=\n')
    assert parsed == {"A": "hello", "B": "plain", "C": ""}


def test_update_env_file_preserves_and_chmods(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    update_env_file(path, {"GITHUB_TOKEN": "abc"})
    update_env_file(path, {"OPENROUTER_API_KEY": "xyz"})
    values = read_env_file(path)
    assert values["GITHUB_TOKEN"] == "abc"
    assert values["OPENROUTER_API_KEY"] == "xyz"
    # Empty updates must not clobber existing secrets.
    update_env_file(path, {"GITHUB_TOKEN": ""})
    assert read_env_file(path)["GITHUB_TOKEN"] == "abc"
    assert (path.stat().st_mode & 0o077) == 0  # owner-only permissions


# --- registry --------------------------------------------------------------
def test_parse_slug_variants() -> None:
    assert parse_slug("octocat/Hello-World") == ("octocat", "Hello-World")
    assert parse_slug("https://github.com/octocat/Hello-World.git") == ("octocat", "Hello-World")
    with pytest.raises(ValueError, match="Invalid repository slug"):
        parse_slug("not-a-slug")


def test_registry_add_remove_toggle(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    assert reg.add(RepoEntry(owner="o", name="r")) is True
    assert reg.add(RepoEntry(owner="o", name="r")) is False  # duplicate
    assert reg.set_enabled("o", "r", enabled=False) is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.enabled is False
    assert reg.remove("o", "r") is True
    assert reg.remove("o", "r") is False


def test_registry_checks_roundtrip(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    assert RepoEntry(owner="o", name="r").checks == ""  # default: gate disabled
    assert RepoEntry(owner="o", name="r").autofix == ""  # default: auto-fix disabled
    reg.add(RepoEntry(owner="o", name="r", checks="ruff check . && pytest", autofix="ruff format ."))
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "ruff check . && pytest"
    assert found.autofix == "ruff format ."


def test_registry_update(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    reg.add(
        RepoEntry(
            owner="o",
            name="r",
            default_branch="develop",
            enabled=False,
            added_at="2024-01-01T00:00:00+00:00",
            checks="ruff check . && pytest",
            autofix="ruff format .",
        ),
    )

    # 1. Update only checks -> autofix is unchanged (and vice versa).
    assert reg.update("o", "r", checks="pytest -q") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "pytest -q"
    assert found.autofix == "ruff format ."

    assert reg.update("o", "r", autofix="ruff check --fix .") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "pytest -q"
    assert found.autofix == "ruff check --fix ."

    # 2. Update both checks and autofix in a single call.
    assert reg.update("o", "r", checks="make test", autofix="make format") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "make test"
    assert found.autofix == "make format"

    # 3. Setting checks="" (or autofix="") clears the value.
    assert reg.update("o", "r", checks="", autofix="") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == ""
    assert found.autofix == ""

    # 4. Updating a slug that isn't registered returns False and does not
    # create the file.
    missing_path = tmp_path / "missing.json"
    missing_reg = Registry(missing_path)
    assert missing_reg.update("unknown", "thing", checks="x") is False
    assert not missing_path.exists()

    # 5. update leaves enabled, default_branch, and added_at untouched when
    # called with only checks / autofix.
    assert reg.update("o", "r", checks="make lint", autofix="make fmt") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.enabled is False
    assert found.default_branch == "develop"
    assert found.added_at == "2024-01-01T00:00:00+00:00"

    # 6. Calling update with no kwargs (or only None kwargs) returns False
    # (no change), does not change any field, and does not rewrite the file.
    mtime_before = reg.path.stat().st_mtime_ns
    assert reg.update("o", "r") is False
    assert reg.update("o", "r", checks=None, autofix=None) is False
    mtime_after = reg.path.stat().st_mtime_ns
    assert mtime_before == mtime_after
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "make lint"
    assert found.autofix == "make fmt"
