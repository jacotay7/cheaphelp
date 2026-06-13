"""Workspace and configuration management for cheaphelp.

The *workspace* is a directory on the user's machine (default `~/.cheaphelp`,
overridable with the `CHEAPHELP_HOME` environment variable) that holds all
machine-local state: secrets, the registry of GitHub repositories, the opencode
configuration and agent definitions, per-issue state, logs and repo clones.

Nothing here talks to the network; this module only owns paths, defaults and the
JSON config file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
"""Bump when the on-disk config layout changes in a breaking way."""

DEFAULT_MODELS: dict[str, str] = {
    # Cheap OpenRouter test tier. Swap these for frontier models once the
    # pipeline works end-to-end. Strings are opencode model ids: openrouter/<id>.
    "responder": "openrouter/google/gemini-2.0-flash-001",
    "planner": "openrouter/deepseek/deepseek-chat",
    "worker": "openrouter/qwen/qwen-2.5-coder-32b-instruct",
    "reviewer": "openrouter/google/gemini-2.0-flash-001",
}

DEFAULT_LABELS: dict[str, str] = {
    "ready": "cheaphelp:ready",
    "rejected": "cheaphelp:rejected",
    "in_progress": "cheaphelp:in-progress",
}

DEFAULT_POLL_INTERVAL = "10m"


def default_home() -> Path:
    """Return the workspace directory, honouring `CHEAPHELP_HOME`."""
    override = os.environ.get("CHEAPHELP_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".cheaphelp"


@dataclass
class Config:
    """The contents of `<workspace>/config.json`."""

    version: int = CONFIG_VERSION
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    poll_interval: str = DEFAULT_POLL_INTERVAL
    opencode_bin: str = "opencode"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Build a `Config`, filling missing keys with defaults."""
        return cls(
            version=int(data.get("version", CONFIG_VERSION)),
            models={**DEFAULT_MODELS, **(data.get("models") or {})},
            labels={**DEFAULT_LABELS, **(data.get("labels") or {})},
            poll_interval=str(data.get("poll_interval", DEFAULT_POLL_INTERVAL)),
            opencode_bin=str(data.get("opencode_bin", "opencode")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON storage."""
        return {
            "version": self.version,
            "models": self.models,
            "labels": self.labels,
            "poll_interval": self.poll_interval,
            "opencode_bin": self.opencode_bin,
        }

    def model_for(self, role: str) -> str:
        """Return the configured model id for an agent role."""
        try:
            return self.models[role]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"No model configured for role {role!r}") from exc


class Workspace:
    """Filesystem layout of a cheaphelp workspace.

    This object is cheap to construct and does not touch disk until you call
    :meth:`ensure` (to create directories) or one of the load/save helpers.
    """

    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or default_home()).expanduser()

    # --- paths -------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def env_path(self) -> Path:
        return self.home / ".env"

    @property
    def registry_path(self) -> Path:
        return self.home / "repos.json"

    @property
    def opencode_dir(self) -> Path:
        return self.home / "opencode"

    @property
    def opencode_config_path(self) -> Path:
        return self.opencode_dir / "opencode.json"

    @property
    def prompts_dir(self) -> Path:
        return self.home / "agents"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def clones_dir(self) -> Path:
        return self.home / "clones"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def all_dirs(self) -> list[Path]:
        return [
            self.home,
            self.opencode_dir,
            self.prompts_dir,
            self.state_dir,
            self.clones_dir,
            self.logs_dir,
        ]

    # --- lifecycle ---------------------------------------------------------
    def exists(self) -> bool:
        """Whether the workspace has been initialised."""
        return self.config_path.exists()

    def ensure(self) -> None:
        """Create the workspace directory tree if missing."""
        for directory in self.all_dirs:
            directory.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> Config:
        """Load `config.json`, or return defaults if it does not exist."""
        if not self.config_path.exists():
            return Config()
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        return Config.from_dict(data)

    def save_config(self, config: Config) -> None:
        """Write `config.json` (pretty-printed, trailing newline)."""
        self.config_path.write_text(
            json.dumps(config.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )

    def clone_path(self, owner: str, repo: str) -> Path:
        """Local path where a registered repository is cloned."""
        return self.clones_dir / f"{owner}__{repo}"

    def issue_state_path(self, owner: str, repo: str, number: int) -> Path:
        """Path to the per-issue state file."""
        return self.state_dir / f"{owner}__{repo}" / f"issue-{number}.json"
