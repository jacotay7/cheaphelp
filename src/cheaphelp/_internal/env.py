"""Secrets handling: the workspace `.env` file.

Personal access tokens never live in the repository or in `config.json`. They go
in `<workspace>/.env`, which is created with `0600` permissions. This module
reads/writes that file with a tiny `KEY=VALUE` parser (no third-party dep) and
exposes helpers to load the values into the process environment so child
processes (opencode, git) inherit them.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

GITHUB_TOKEN_KEY = "GITHUB_TOKEN"
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"

# Keys we manage in the workspace .env, in a stable order.
MANAGED_KEYS = (GITHUB_TOKEN_KEY, OPENROUTER_API_KEY)

# Minimum length for a value to possibly be wrapped in a matching quote pair.
_QUOTE_PAIR_LEN = 2


def parse_env(text: str) -> dict[str, str]:
    """Parse a `.env`-style string into a dict.

    Supports `KEY=VALUE`, blank lines and `#` comments. Surrounding single or
    double quotes around the value are stripped. `export ` prefixes are ignored.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= _QUOTE_PAIR_LEN and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def read_env_file(path: Path) -> dict[str, str]:
    """Read a `.env` file, returning an empty dict if it is missing."""
    if not path.exists():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write `values` to a `.env` file with `0600` permissions.

    Managed keys are written first (always present, even if empty) so the file
    documents what cheaphelp expects; any extra keys follow.
    """
    lines = [
        "# cheaphelp secrets - keep this file private. Do not commit it.",
        "# GITHUB_TOKEN     : a GitHub personal access token (repo + workflow + issues scope).",
        "# OPENROUTER_API_KEY: your OpenRouter API key.",
        "",
    ]
    for key in MANAGED_KEYS:
        lines.append(f"{key}={values.get(key, '')}")
    for key, value in values.items():
        if key not in MANAGED_KEYS:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Restrict permissions: owner read/write only.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def update_env_file(path: Path, updates: dict[str, str]) -> dict[str, str]:
    """Merge `updates` into an existing `.env` file and persist it.

    Empty-string updates are ignored so callers can pass through unset CLI
    options without clobbering existing secrets. Returns the merged values.
    """
    values = read_env_file(path)
    for key, value in updates.items():
        if value:
            values[key] = value
    write_env_file(path, values)
    return values


def load_into_environ(path: Path, *, override: bool = False) -> dict[str, str]:
    """Load a `.env` file into `os.environ` and return the parsed values.

    Existing environment variables win unless `override` is set.
    """
    values = read_env_file(path)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values
