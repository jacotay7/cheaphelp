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
    # Two-tier OpenRouter setup (verified live, 2026-06). Strings are opencode
    # model ids: openrouter/<openrouter-model-id>.
    #   - cheap tier  (deepseek-v4-flash): high-volume conversational work and
    #     per-task implementation (run with the `max` variant; see below).
    #   - better tier (minimax-m3):        planning and review.
    # The worker runs on the cheap tier (`max` variant) because each task is
    # small and well-specified by the planner; this keeps ticks fast/cheap and
    # avoids the slow minimax timeouts seen in field testing.
    "responder": "openrouter/deepseek/deepseek-v4-flash",
    "planner": "openrouter/minimax/minimax-m3",
    "worker": "openrouter/deepseek/deepseek-v4-flash",
    "reviewer": "openrouter/minimax/minimax-m3",
}

DEFAULT_LABELS: dict[str, str] = {
    "ready": "cheaphelp:ready",  # responder finalized; planner's input
    "rejected": "cheaphelp:rejected",  # responder declined
    "planned": "cheaphelp:planned",  # planner produced tasks; worker's input
    "in_progress": "cheaphelp:in-progress",  # worker is executing tasks
    "in_review": "cheaphelp:in-review",  # reviewer opened a PR; awaiting human
    "needs_replan": "cheaphelp:needs-replan",  # reviewer sent it back to the planner
    "needs_human": "cheaphelp:needs-human",  # stuck; a person should look
}

# Labels that mean "the responder should leave this issue alone" — it has moved
# past the conversation stage into the build pipeline (or was rejected).
RESPONDER_DONE_LABEL_KEYS = (
    "ready",
    "rejected",
    "planned",
    "in_progress",
    "in_review",
    "needs_replan",
    "needs_human",
)

DEFAULT_POLL_INTERVAL = "10m"
DEFAULT_AGENT_TIMEOUT = 600.0
"""Default per-agent subprocess timeout (seconds) for `opencode.run_agent`."""

# Per-role opencode model "variant" (provider-specific reasoning effort, passed
# as `--variant`). Empty string = the provider's default. `deepseek-v4-flash`
# with the `max` variant is the "deepseek-v4-flash-max" cheap-but-strong tier.
DEFAULT_VARIANTS: dict[str, str] = {
    "responder": "max",
    "planner": "",
    "worker": "max",
    "reviewer": "",
}

# Sandboxing knobs for the agents. These drive opencode's permission system
# (see opencode.py). They are guardrails / defence-in-depth, not a true OS
# sandbox — for strong isolation run the whole thing under a container or a
# low-privilege user (see the README).
DEFAULT_SANDBOX: dict[str, bool] = {
    # Confine the edit/read tools to the working directory (the clone): sets
    # opencode's `external_directory` permission to "deny".
    "confine_to_workdir": True,
    # Apply the curated bash allow/deny policy (readers: deny-by-default with a
    # read-only allowlist; worker: allow with a dangerous-command denylist).
    "restrict_bash": True,
    # Disallow web access (webfetch/websearch) for all agents.
    "no_network_tools": True,
}


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
    variants: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_VARIANTS))
    labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    sandbox: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_SANDBOX))
    # GitHub usernames to request as reviewers on opened PRs. Empty = default to
    # the repository owner.
    pr_reviewers: list[str] = field(default_factory=list)
    poll_interval: str = DEFAULT_POLL_INTERVAL
    opencode_bin: str = "opencode"
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT
    # Cap on issues processed per repo per tick (0 = unlimited). A CLI
    # `--max-issues` flag overrides this when > 0.
    max_issues_per_tick: int = 0
    # Cap on worker tasks run per issue per tick (0 = unlimited). Keeps a single
    # tick bounded/predictable; remaining tasks resume on the next tick since
    # task state is persisted.
    max_tasks_per_tick: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Build a `Config`, filling missing keys with defaults."""
        return cls(
            version=int(data.get("version", CONFIG_VERSION)),
            models={**DEFAULT_MODELS, **(data.get("models") or {})},
            variants={**DEFAULT_VARIANTS, **(data.get("variants") or {})},
            labels={**DEFAULT_LABELS, **(data.get("labels") or {})},
            sandbox={**DEFAULT_SANDBOX, **(data.get("sandbox") or {})},
            pr_reviewers=list(data.get("pr_reviewers") or []),
            poll_interval=str(data.get("poll_interval", DEFAULT_POLL_INTERVAL)),
            opencode_bin=str(data.get("opencode_bin", "opencode")),
            agent_timeout=float(data.get("agent_timeout", DEFAULT_AGENT_TIMEOUT)),
            max_issues_per_tick=int(data.get("max_issues_per_tick", 0)),
            max_tasks_per_tick=int(data.get("max_tasks_per_tick", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON storage."""
        return {
            "version": self.version,
            "models": self.models,
            "variants": self.variants,
            "labels": self.labels,
            "sandbox": self.sandbox,
            "pr_reviewers": self.pr_reviewers,
            "poll_interval": self.poll_interval,
            "opencode_bin": self.opencode_bin,
            "agent_timeout": self.agent_timeout,
            "max_issues_per_tick": self.max_issues_per_tick,
            "max_tasks_per_tick": self.max_tasks_per_tick,
        }

    def model_for(self, role: str) -> str:
        """Return the configured model id for an agent role."""
        try:
            return self.models[role]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"No model configured for role {role!r}") from exc

    def variant_for(self, role: str) -> str:
        """Return the opencode `--variant` for a role, or "" for the default."""
        return (self.variants or {}).get(role, "") or ""


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
    def run_lock_path(self) -> Path:
        """Path of the `fcntl`-locked file that serialises orchestrator ticks."""
        return self.home / "run.lock"

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
        """Local path of the shared read-only clone for a repository."""
        return self.clones_dir / f"{owner}__{repo}"

    def work_clone_path(self, owner: str, repo: str, number: int) -> Path:
        """Local path of the persistent build clone for one issue."""
        return self.clones_dir / f"{owner}__{repo}__issue-{number}"

    def issue_state_path(self, owner: str, repo: str, number: int) -> Path:
        """Path to the per-issue state marker file."""
        return self.state_dir / f"{owner}__{repo}" / f"issue-{number}.json"

    def issue_dir(self, owner: str, repo: str, number: int) -> Path:
        """Directory holding an issue's spec, plan and tasks."""
        return self.state_dir / f"{owner}__{repo}" / f"issue-{number}"
