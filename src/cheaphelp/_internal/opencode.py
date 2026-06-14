"""Integration with the opencode CLI harness.

cheaphelp does not implement its own agent loop. It shells out to `opencode`
(https://opencode.ai), a provider-agnostic terminal coding agent, configured to
talk to OpenRouter. Each cheaphelp role (responder, planner, worker, reviewer)
becomes an opencode *agent* with its own model, system prompt and tool
permissions, all generated into a single `opencode.json`.

This module generates that config and provides :func:`run_agent`, a headless
invocation that returns the agent's text output plus any parsed decision block.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.templates import AGENT_ROLES, load_all_prompts

OPENCODE_SCHEMA = "https://opencode.ai/config.json"

# Roles that are allowed to modify files. The responder is strictly read-only;
# it converses, it does not change the codebase.
_WRITER_ROLES = {"worker", "rework"}

# Mirrors what opencode expects: model ids are "openrouter/<openrouter-model-id>".
_OPENROUTER_PREFIX = "openrouter/"

# --- bash sandbox policies -------------------------------------------------
# opencode matches bash commands against these glob patterns; the LAST matching
# rule wins, so the broad "*" rule comes first. These are guardrails, not a true
# sandbox: a determined agent can evade pattern matching (e.g. via a script
# interpreter). For real isolation run under a container or low-privilege user.

# Readers (responder, planner, reviewer): deny by default, allow read-only probes.
# They mostly use opencode's built-in read/grep/glob tools, which are separate.
_READER_BASH: dict[str, str] = {
    "*": "deny",
    "ls*": "allow",
    "cat *": "allow",
    "head *": "allow",
    "tail *": "allow",
    "wc *": "allow",
    "grep *": "allow",
    "rg *": "allow",
    "find *": "allow",
    "tree*": "allow",
    "pwd": "allow",
    "echo *": "allow",
    "git status*": "allow",
    "git log*": "allow",
    "git diff*": "allow",
    "git show*": "allow",
    "git branch*": "allow",
    "git ls-files*": "allow",
}

# Worker (writer): allow by default so it can build and run tests, but deny
# clearly dangerous or out-of-scope commands. We do git push ourselves, so the
# agent is not allowed to touch remotes.
_WORKER_BASH: dict[str, str] = {
    "*": "allow",
    "sudo*": "deny",
    "su *": "deny",
    "rm -rf /*": "deny",
    "rm -rf ~*": "deny",
    "rm -fr /*": "deny",
    "shutdown*": "deny",
    "reboot*": "deny",
    "halt*": "deny",
    "mkfs*": "deny",
    "dd *": "deny",
    "chmod -R *": "deny",
    "chown -R *": "deny",
    "ssh *": "deny",
    "scp *": "deny",
    "curl *|*": "deny",
    "wget *|*": "deny",
    "git push*": "deny",
    "git remote*": "deny",
    "crontab*": "deny",
}


def _permission_for(role: str, sandbox: dict[str, bool]) -> dict:
    """Build the opencode `permission` block for an agent from sandbox config."""
    is_writer = role in _WRITER_ROLES
    perm: dict = {"edit": "allow" if is_writer else "deny"}
    if sandbox.get("no_network_tools", True):
        perm["webfetch"] = "deny"
        perm["websearch"] = "deny"
    if sandbox.get("confine_to_workdir", True):
        # Keep file read/edit tools inside the working directory (the clone).
        perm["external_directory"] = "deny"
    if sandbox.get("restrict_bash", True):
        perm["bash"] = dict(_WORKER_BASH if is_writer else _READER_BASH)
    else:
        perm["bash"] = "allow"
    return perm


@dataclass
class AgentResult:
    """Outcome of a headless opencode invocation."""

    returncode: int
    stdout: str
    stderr: str
    decision: dict | None  # the parsed final ```json block, if present

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# Common locations the opencode installer drops the binary that are not always
# on a non-interactive shell's PATH.
_OPENCODE_FALLBACK_PATHS = (
    "~/.opencode/bin/opencode",
    "~/.local/bin/opencode",
)


def find_opencode(config: Config) -> str | None:
    """Return the path to the opencode binary.

    Tries the configured name on PATH first (so an absolute path or a custom
    name works), then a few well-known install locations. Returns None if not
    found anywhere.
    """
    found = shutil.which(config.opencode_bin)
    if found:
        return found
    # Only probe fallbacks for the default name; a custom name is intentional.
    if config.opencode_bin == "opencode":
        for candidate in _OPENCODE_FALLBACK_PATHS:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return None


def _strip_prefix(model: str) -> str:
    return model.removeprefix(_OPENROUTER_PREFIX)


def load_workspace_prompts(workspace: Workspace) -> dict[str, str]:
    """Load agent prompts from the workspace, falling back to bundled copies.

    The workspace `agents/<role>.md` files are the source of truth so users can
    tune agent behaviour; any role without a workspace file uses the bundled one.
    """
    bundled = load_all_prompts()
    prompts: dict[str, str] = {}
    for role in AGENT_ROLES:
        override = workspace.prompts_dir / f"{role}.md"
        if override.exists():
            prompts[role] = override.read_text(encoding="utf-8")
        else:
            prompts[role] = bundled[role]
    return prompts


def write_workspace_prompts(workspace: Workspace, *, overwrite: bool = False) -> list[str]:
    """Copy bundled prompts into the workspace so users can edit them.

    Existing files are kept unless `overwrite` is set. Returns the roles written.
    """
    workspace.prompts_dir.mkdir(parents=True, exist_ok=True)
    bundled = load_all_prompts()
    written: list[str] = []
    for role, text in bundled.items():
        target = workspace.prompts_dir / f"{role}.md"
        if overwrite or not target.exists():
            target.write_text(text, encoding="utf-8")
            written.append(role)
    return written


def build_opencode_config(config: Config, prompts: dict[str, str] | None = None) -> dict:
    """Build the `opencode.json` document from cheaphelp config + prompts.

    Every role becomes an agent with its model, prompt and tool permissions.
    The OpenRouter provider lists each configured model so opencode preloads it.
    """
    if prompts is None:
        prompts = load_all_prompts()

    provider_models: dict[str, dict] = {}
    for model in config.models.values():
        provider_models[_strip_prefix(model)] = {}

    agents: dict[str, dict] = {}
    for role in AGENT_ROLES:
        is_writer = role in _WRITER_ROLES
        agents[role] = {
            "description": f"cheaphelp {role} agent",
            "mode": "all",
            "model": config.model_for(role),
            "temperature": 0.2 if is_writer else 0.3,
            "prompt": prompts[role],
            "tools": {
                "write": is_writer,
                "edit": is_writer,
            },
            # Explicit permissions so headless `run` never blocks on a prompt,
            # and the sandbox policy confines/limits what each agent can do.
            "permission": _permission_for(role, config.sandbox),
        }

    return {
        "$schema": OPENCODE_SCHEMA,
        "model": config.model_for("responder"),
        "provider": {
            "openrouter": {
                "models": provider_models,
            },
        },
        "agent": agents,
    }


def write_opencode_config(workspace: Workspace, config: Config) -> Path:
    """Generate and persist `<workspace>/opencode/opencode.json`."""
    workspace.opencode_dir.mkdir(parents=True, exist_ok=True)
    document = build_opencode_config(config, load_workspace_prompts(workspace))
    path = workspace.opencode_config_path
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


# Match the LAST fenced ```json block in a string.
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def extract_decision(text: str) -> dict | None:
    """Extract the final ```json decision block from agent output.

    Returns the parsed object, or None if no valid JSON block is found.
    """
    matches = _JSON_BLOCK_RE.findall(text)
    for candidate in reversed(matches):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    # Fall back: maybe the whole output is a bare JSON object.
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None
    return None


def _mock_result() -> AgentResult | None:
    """Return a canned result when CHEAPHELP_AGENT_MOCK points to a JSON file.

    This lets the orchestrator be exercised end-to-end without opencode or any
    API key: the file's contents are treated as the agent's final decision.
    """
    mock_path = os.environ.get("CHEAPHELP_AGENT_MOCK")
    if not mock_path:
        return None
    text = Path(mock_path).read_text(encoding="utf-8")
    return AgentResult(returncode=0, stdout=text, stderr="", decision=extract_decision(text))


_REPROMPT_SUFFIX = (
    "Reminder: your entire reply must be EXACTLY ONE fenced ```json code block "
    "and nothing else — no prose before or after it. Reply with only that block now."
)


def run_agent(
    workspace: Workspace,
    config: Config,
    role: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: float = 600.0,
) -> AgentResult:
    """Run an opencode agent headlessly and return its result.

    Parameters:
        workspace: the cheaphelp workspace (provides the opencode config path).
        config: cheaphelp config (model selection, binary name).
        role: the agent role to invoke (e.g. "responder").
        prompt: the user message handed to the agent.
        cwd: working directory for opencode (usually a repo clone).
        timeout: seconds before the subprocess is killed.

    If `CHEAPHELP_AGENT_MOCK` is set, no subprocess runs; the mock is returned.
    """
    mock = _mock_result()
    if mock is not None:
        return mock

    binary = find_opencode(config)
    if binary is None:
        raise FileNotFoundError(
            f"opencode binary {config.opencode_bin!r} not found on PATH. "
            "Install it (https://opencode.ai) or set opencode_bin in config.json.",
        )

    env = dict(os.environ)
    env["OPENCODE_CONFIG"] = str(workspace.opencode_config_path)
    # opencode resolves its project directory from $PWD, which subprocess(cwd=...)
    # does NOT update — set it explicitly and also pass --dir, or opencode will
    # operate on the parent process's directory instead of the clone.
    env["PWD"] = str(cwd)

    base_command = [
        binary,
        "run",
        "--dir",
        str(cwd),
        "--agent",
        role,
        "--model",
        config.model_for(role),
    ]
    variant = config.variant_for(role)
    if variant:
        base_command += ["--variant", variant]

    def _invoke(prompt_text: str) -> AgentResult:
        proc = subprocess.run(
            [*base_command, prompt_text],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return AgentResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            decision=extract_decision(proc.stdout),
        )

    result = _invoke(prompt)
    # The contract is a single final ```json block. If the agent exited cleanly
    # but we couldn't parse one, give it exactly one more chance with a pointed
    # reminder before the caller treats the turn as a failure.
    if result.decision is None and result.returncode == 0:
        result = _invoke(f"{prompt}\n\n{_REPROMPT_SUFFIX}")
    return result
