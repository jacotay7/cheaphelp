"""Tests for cheaphelp's `config` subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cheaphelp import main
from cheaphelp._internal.config import Workspace
from tests.conftest import _setup_workspace


# --- config ------------------------------------------------------------------
def test_config_show_prints_header_and_default_marker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config show` prints header, scalars and a ``(default)`` marker."""
    ws = Workspace(tmp_path)
    ws.ensure()
    # Write a minimal config so un-set keys show the (default) marker.
    ws.config_path.write_text(json.dumps({"version": 1}) + "\n")
    rc = main(["--home", str(ws.home), "config", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# " in out
    assert "config.json" in out.splitlines()[0]
    assert "agent_timeout:" in out
    assert "(default)" in out


def test_config_get_scalar_returns_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config get agent_timeout` prints ``600.0`` and exits 0."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "get", "agent_timeout"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "600.0"


def test_config_get_models_subkey(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config get models.worker` prints the model string and exits 0."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "get", "models.worker"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "deepseek" in out


def test_config_get_unknown_key_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config get nonexistent.key` exits 2 with an error on stderr."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "get", "nonexistent.key"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Unknown config key: nonexistent" in err


def test_config_set_scalar_persists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config set agent_timeout 300` persists to config.json."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "set", "agent_timeout", "300"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Set agent_timeout" in out

    config = ws.load_config()
    assert config.agent_timeout == 300.0

    raw = json.loads(ws.config_path.read_text(encoding="utf-8"))
    assert raw["agent_timeout"] == 300.0


def test_config_set_models_regenerates_opencode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config set models.worker` updates the model and regenerates opencode.json."""
    ws = _setup_workspace(tmp_path)
    rc = main(
        [
            "--home",
            str(ws.home),
            "config",
            "set",
            "models.worker",
            "openrouter/some/model",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Set models.worker" in out
    assert "Regenerated" in out

    assert ws.opencode_config_path.exists()
    opencode_cfg = json.loads(ws.opencode_config_path.read_text(encoding="utf-8"))
    assert opencode_cfg["agent"]["worker"]["model"] == "openrouter/some/model"


def test_config_set_bool(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config set prune_work_clones false` correctly sets the bool."""
    ws = _setup_workspace(tmp_path)

    rc = main(
        [
            "--home",
            str(ws.home),
            "config",
            "set",
            "prune_work_clones",
            "false",
        ],
    )
    assert rc == 0
    capsys.readouterr()  # discard output
    config = ws.load_config()
    assert config.prune_work_clones is False

    # Round-trip: set back to true.
    rc = main(
        [
            "--home",
            str(ws.home),
            "config",
            "set",
            "prune_work_clones",
            "true",
        ],
    )
    assert rc == 0
    capsys.readouterr()  # discard output
    config = ws.load_config()
    assert config.prune_work_clones is True


def test_config_set_type_mismatch_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config set agent_timeout notanumber` exits 2 and does not change the value."""
    ws = _setup_workspace(tmp_path)
    assert ws.load_config().agent_timeout == 600.0

    rc = main(
        [
            "--home",
            str(ws.home),
            "config",
            "set",
            "agent_timeout",
            "notanumber",
        ],
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "Expected float" in err

    # Value must not have changed.
    assert ws.load_config().agent_timeout == 600.0


def test_config_show_includes_models_and_variants_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config show` lists both ``models:`` and ``variants:`` sections with roles."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "models:" in out
    assert "variants:" in out
    # At least one role appears under each section (default config has "worker" in both).
    assert "worker:" in out


def test_config_get_variants_subkey(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config get variants.worker` prints the variant and exits 0."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "get", "variants.worker"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "max" in out


def test_config_set_variants_regenerates_opencode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config set variants.worker` triggers opencode.json regeneration."""
    ws = _setup_workspace(tmp_path)
    rc = main(
        [
            "--home",
            str(ws.home),
            "config",
            "set",
            "variants.worker",
            "bogusvariant",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Set variants.worker" in out
    assert "Regenerated" in out

    # Confirm the variant was written.
    config = ws.load_config()
    assert config.variants["worker"] == "bogusvariant"


def test_config_no_workspace_exits_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config show` on an uninitialised workspace exits 1 with ``No workspace``."""
    ws = Workspace(tmp_path)  # NOTE: no ws.ensure() / ws.save_config()
    rc = main(["--home", str(ws.home), "config", "show"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No workspace" in err


# --- budget config CLI tests -------------------------------------------------
def test_config_set_daily_budget_usd_persists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config set daily_budget_usd 5.0` persists to config.json."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "set", "daily_budget_usd", "5.0"])
    assert rc == 0
    capsys.readouterr()  # discard output
    config = ws.load_config()
    assert config.daily_budget_usd == 5.0


def test_config_get_daily_budget_usd_returns_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config get daily_budget_usd` prints the configured value and exits 0."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "set", "daily_budget_usd", "5.0"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["--home", str(ws.home), "config", "get", "daily_budget_usd"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "5.0"


def test_config_set_budget_warn_at_persists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config set budget_warn_at 0.5` persists to config.json."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "set", "budget_warn_at", "0.5"])
    assert rc == 0
    capsys.readouterr()  # discard output
    config = ws.load_config()
    assert config.budget_warn_at == 0.5


def test_config_show_includes_budget_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`config show` output contains both `daily_budget_usd:` and `budget_warn_at:`."""
    ws = _setup_workspace(tmp_path)
    rc = main(["--home", str(ws.home), "config", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "daily_budget_usd:" in out
    assert "budget_warn_at:" in out
