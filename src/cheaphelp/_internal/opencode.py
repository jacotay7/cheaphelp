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

import contextlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.templates import AGENT_ROLES, load_all_prompts

_LOG = logging.getLogger(__name__)

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
class UsageData:
    """Token usage and estimated cost from an OpenRouter API call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: UsageData) -> UsageData:
        return UsageData(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def __iadd__(self, other: UsageData) -> UsageData:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cost_usd += other.cost_usd
        return self

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict) -> UsageData:
        """Build from a dict, accepting both ``cost`` and ``cost_usd`` keys."""
        return cls(
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            cost_usd=float(data.get("cost_usd", data.get("cost", 0.0))),
        )


@dataclass
class AgentResult:
    """Outcome of a headless opencode invocation."""

    returncode: int
    stdout: str
    stderr: str
    decision: dict | None  # the parsed final ```json block, if present
    usage: UsageData | None = None

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


def _parse_usage(stdout: str, stderr: str) -> UsageData | None:
    """Parse token usage / cost data from opencode's stderr or stdout.

    Tries *stderr* first, then *stdout*. For each source, attempts
    ``json.loads`` on the whole string and, if that fails, on each line
    that starts with ``{`` independently.  A parsed object is considered a
    usage payload when it is a dict that either has a nested ``usage`` dict
    or itself contains one of ``prompt_tokens``, ``completion_tokens``,
    ``cost``, ``cost_usd``.  Returns ``None`` when nothing parseable is
    found.
    """
    for source in (stderr, stdout):
        if not source:
            continue

        data: object | None = None
        # Try the whole string first.
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(source)

        if data is None:
            # Fall back: each line that starts with '{'
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    try:
                        data = json.loads(stripped)
                        break
                    except json.JSONDecodeError:
                        continue

        if isinstance(data, dict):
            # Nested "usage" key takes precedence.
            usage_dict = data.get("usage")
            if isinstance(usage_dict, dict):
                return UsageData.from_dict(usage_dict)
            # Otherwise the dict itself must contain usage fields.
            if data.keys() & {"prompt_tokens", "completion_tokens", "cost", "cost_usd"}:
                return UsageData.from_dict(data)

    return None


# Match ```json fence openings; balanced-brace scanner finds the matching close.
_JSON_FENCE_RE = re.compile(r"```json\b")


def _find_balanced_json_object(text: str, start: int) -> int | None:
    """Walk from ``text[start] == "{"`` and return index past matching ``}``.

    Tracks string/escape state so braces inside JSON string values are
    correctly ignored.  Returns ``None`` if the text ends before the
    matching close brace.

    Parameters:
        text: the full output text being scanned.
        start: index of the opening ``{`` character.

    Returns:
        Index *one past* the matching ``}``, or ``None`` if unbalanced.
    """
    in_string = False
    escape = False
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if escape:
            escape = False
        elif ch == "\\" and in_string:
            escape = True
        elif ch == '"' and not in_string:
            in_string = True
        elif ch == '"' and in_string:
            in_string = False
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return None


def _find_json_block_candidates(text: str) -> list[str]:
    """Return every properly-fenced JSON block from *text*.

    Locates `` ```json `` fences with a regex, finds the matching balanced
    brace pair with ``_find_balanced_json_object``, and extracts the JSON
    string.  Returns an empty list when no valid fenced block is found.

    Parameters:
        text: the full agent output string to scan.

    Returns:
        List of JSON object strings (without fences) for every candidate.
    """
    candidates: list[str] = []
    for match in _JSON_FENCE_RE.finditer(text):
        k = match.end()
        # Skip ASCII whitespace after the opening fence.
        while k < len(text) and text[k] in " \t\n\r\f\v":
            k += 1
        # Must be followed by a '{'.
        if k >= len(text) or text[k] != "{":
            continue
        end = _find_balanced_json_object(text, k)
        if end is None:
            continue
        # Skip ASCII whitespace after the close brace.
        m = end
        while m < len(text) and text[m] in " \t\n\r\f\v":
            m += 1
        # Must be followed by closing ```
        if text.startswith("```", m):
            candidates.append(text[k:end])
    return candidates


def extract_decision(text: str) -> dict | None:
    """Extract the final ```json decision block from agent output.

    Uses a two-step approach: a regex locates `` ```json `` fence openings,
    then a balanced-brace character scanner finds the matching ``}`` so
    that braces inside JSON string values do not cause truncation.
    Returns the parsed dict, or ``None`` if no valid JSON block is found.
    """
    candidates = _find_json_block_candidates(text)
    for candidate in reversed(candidates):
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


def _compute_backoff(attempt: int, base_delay: float) -> float:
    """Return the backoff for `attempt` (1-indexed) with ±25% jitter."""
    base = base_delay * (2 ** (attempt - 1))
    jitter = base * 0.25 * (2 * random.random() - 1)  # noqa: S311
    return max(0.0, base + jitter)


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

# Maximum bytes to keep from unparseable agent output (last N bytes).
_UNPARSED_LOG_BYTES = 65_536


def _save_unparsed_output(
    issue_dir: Path | None,
    role: str,
    result: AgentResult,
) -> None:
    """Persist raw agent output when parsing failed on a clean exit.

    Writes ``last_unparsed_<role>.log`` to *issue_dir* when the agent exited
    cleanly (returncode 0) but produced no parseable decision block.  This is a
    best-effort diagnostic helper — I/O errors are logged and swallowed.

    Parameters:
        issue_dir: the issue state directory (``None`` to skip).
        role: the agent role name (e.g. ``"worker"``).
        result: the agent result to inspect and persist.
    """
    if issue_dir is None:
        return
    if result.returncode != 0:
        return
    if result.decision is not None:
        return

    try:
        parts: list[str] = []
        if result.stdout:
            parts.append("--- stdout ---")
            parts.append(result.stdout)
        if result.stderr:
            parts.append("--- stderr ---")
            parts.append(result.stderr)

        body = "\n".join(parts) if parts else "(no output captured)\n"

        encoded = body.encode("utf-8")
        if len(encoded) > _UNPARSED_LOG_BYTES:
            encoded = encoded[-_UNPARSED_LOG_BYTES:]
            body = encoded.decode("utf-8", errors="replace")
            body = f"... (truncated to last {_UNPARSED_LOG_BYTES} bytes) ...\n{body}"

        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / f"last_unparsed_{role}.log").write_text(body, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "failed to save unparsed output for %s in %s: %s",
            role,
            issue_dir,
            exc,
        )


def run_agent(
    workspace: Workspace,
    config: Config,
    role: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: float = 600.0,
    issue_dir: Path | None = None,
) -> AgentResult:
    """Run an opencode agent headlessly and return its result.

    Parameters:
        workspace: the cheaphelp workspace (provides the opencode config path).
        config: cheaphelp config (model selection, binary name).
        role: the agent role to invoke (e.g. "responder").
        prompt: the user message handed to the agent.
        cwd: working directory for opencode (usually a repo clone).
        timeout: seconds before the subprocess is killed.
        issue_dir: issue state directory; when set, unparseable clean-exit
            output is saved to ``last_unparsed_<role>.log`` in this directory
            for offline diagnosis.

    If `CHEAPHELP_AGENT_MOCK` is set, no subprocess runs; the mock is returned.

    Retries with exponential backoff on timeout, non-zero exit, and clean exit
    without a parseable decision block, up to ``retry_attempts`` attempts (per
    config).  On the **final** attempt only, the prompt is appended with the
    format reminder (``_REPROMPT_SUFFIX``) to give the model one last hint
    before the output is declared unparseable.
    """
    mock = _mock_result()
    if mock is not None:
        _save_unparsed_output(issue_dir, role, mock)
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
        result = AgentResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            decision=extract_decision(proc.stdout),
        )
        result.usage = _parse_usage(result.stdout, result.stderr)
        return result

    max_attempts = max(1, config.retry_attempts)
    for attempt in range(1, max_attempts + 1):
        is_last = attempt >= max_attempts
        current_prompt = f"{prompt}\n\n{_REPROMPT_SUFFIX}" if is_last else prompt
        try:
            result = _invoke(current_prompt)
        except subprocess.TimeoutExpired:
            if attempt >= max_attempts:
                raise
            delay = _compute_backoff(attempt, config.retry_base_delay)
            _LOG.warning(
                "agent %s: attempt %d/%d failed: timeout, retrying in %.1fs",
                role,
                attempt,
                max_attempts,
                delay,
            )
            time.sleep(delay)
            continue

        if result.returncode != 0:
            if attempt >= max_attempts:
                _save_unparsed_output(issue_dir, role, result)
                return result
            delay = _compute_backoff(attempt, config.retry_base_delay)
            _LOG.warning(
                "agent %s: attempt %d/%d failed: exit %d, retrying in %.1fs",
                role,
                attempt,
                max_attempts,
                result.returncode,
                delay,
            )
            time.sleep(delay)
            continue

        # Clean exit.
        if result.decision is not None:
            return result

        # Clean exit, no parseable decision: treat as a transient failure and
        # retry with backoff. The format reminder was already sent on the last
        # attempt via `current_prompt`; if we still got nothing back, return the
        # result as-is so the caller sees the unparseable output.
        if is_last:
            _save_unparsed_output(issue_dir, role, result)
            return result
        delay = _compute_backoff(attempt, config.retry_base_delay)
        _LOG.warning(
            "agent %s: attempt %d/%d produced no parseable decision, retrying in %.1fs",
            role,
            attempt,
            max_attempts,
            delay,
        )
        time.sleep(delay)
        continue

    raise RuntimeError("retry loop exited without return")  # pragma: no cover
