"""Fixer turn logic.

When a build finishes but the repository's deterministic quality gate fails, the
fixer makes one (or a few) attempts to repair the working tree from the gate
output before the issue is sent back to the planner. It is a writer role like the
worker: the agent edits files in the issue's persistent work clone, and this
module commits/pushes the result so a passing gate produces a PR from the same
branch.

The orchestrator (`_quality_gate`) drives the loop: run gate, on failure call
:func:`run_fix`, re-run gate, repeat up to ``quality_gate_fix_attempts`` times.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cheaphelp._internal import gitutil, opencode
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.conventions import read_conventions
from cheaphelp._internal.opencode import UsageData
from cheaphelp._internal.registry import RepoEntry
from cheaphelp._internal.worker import branch_name

# Keep the gate output we hand the model bounded; cheap context budgets.
_MAX_OUTPUT_CHARS = 6000


def build_prompt(issue_md: str, check_command: str, check_output: str, *, conventions: str = "") -> str:
    """Render the fixer's user message.

    Parameters:
        issue_md: the original issue spec (for context only).
        check_command: the quality-gate command that failed.
        check_output: the command's combined output (tail kept if long).
        conventions: repository conventions, appended verbatim when non-empty.
    """
    output = check_output
    if len(output) > _MAX_OUTPUT_CHARS:
        output = "... (output truncated) ...\n" + output[-_MAX_OUTPUT_CHARS:]
    lines = [
        "The implementation for this issue is complete, but the repository's "
        "quality gate failed. Repair the working tree so the gate passes.",
        "",
        "## Issue context (for background only — do not re-implement the issue)",
        "",
        issue_md.strip() or "_(no spec)_",
        "",
        "## Failing quality-gate command",
        "",
        "```",
        check_command or "(none)",
        "```",
        "",
        "## Quality-gate output",
        "",
        "```",
        output or "(no output)",
        "```",
    ]
    if conventions.strip():
        lines += [
            "",
            "## Repository conventions",
            "",
            conventions.rstrip(),
        ]
    lines += [
        "",
        "---",
        "",
        "Fix the failure in the working directory, re-run the command to verify, "
        "then report following your output protocol (a single json block).",
    ]
    return "\n".join(lines)


@dataclass
class FixResult:
    """Outcome of a fixer turn, for logging."""

    status: str  # "done" | "blocked" | "unparseable"
    committed: bool = False
    error: str | None = None
    usage: UsageData | None = None


def run_fix(
    workspace: Workspace,
    config: Config,
    repo: RepoEntry,
    number: int,
    check_output: str,
    clone_dir: Path,
    *,
    token: str | None,
) -> FixResult:
    """Run one fixer turn: agent edits to repair the gate, then commit/push.

    The agent's reported status is advisory; the orchestrator re-runs the gate to
    decide whether the fix actually worked. Any changes are committed and pushed
    so the remote branch matches the (hopefully now-passing) working tree.
    """
    issue_dir = workspace.issue_dir(repo.owner, repo.name, number)
    issue_md_path = issue_dir / "issues.md"
    issue_md = issue_md_path.read_text(encoding="utf-8") if issue_md_path.exists() else ""
    conventions = read_conventions(clone_dir)
    prompt = build_prompt(issue_md, repo.checks, check_output, conventions=conventions)

    result = opencode.run_agent(
        workspace,
        config,
        "fixer",
        prompt,
        cwd=clone_dir,
        timeout=config.agent_timeout,
        issue_dir=issue_dir,
    )
    usage = result.usage

    # Commit/push whatever the agent changed regardless of the reported status —
    # the gate re-run is the real arbiter. commit_all is a no-op when the tree is
    # clean, so an empty (or unparseable) turn leaves the branch untouched.
    committed = gitutil.commit_all(clone_dir, message=f"cheaphelp: fix quality gate for #{number}")
    if committed and not os.environ.get("CHEAPHELP_NO_PUSH"):
        gitutil.push_branch(clone_dir, repo, branch=branch_name(number), token=token)

    if result.decision is None:
        return FixResult(status="unparseable", committed=committed, error="unparseable", usage=usage)
    status = str(result.decision.get("status", "")).strip().lower()
    return FixResult(status=status or "done", committed=committed, usage=usage)
