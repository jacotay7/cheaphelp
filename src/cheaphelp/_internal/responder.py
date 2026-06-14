"""Responder turn logic.

The responder converses with issue authors to refine scope. This module decides
*when* an issue needs a responder turn, builds the context handed to the agent,
and applies the agent's decision back to GitHub and the workspace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cheaphelp._internal.config import RESPONDER_DONE_LABEL_KEYS, Config, Workspace
from cheaphelp._internal.github import Comment, GitHubClient, Issue

# Hidden marker embedded in every message cheaphelp posts, so we can always
# recognise our own comments regardless of which account the token belongs to.
BOT_MARKER = "<!-- cheaphelp:responder -->"

# Visible prefix of the attribution header. Kept as a stable constant so readers
# (and models) can match on it to spot machine-generated messages.
ATTRIBUTION_PREFIX = "🤖 cheaphelp"

VALID_ACTIONS = {"comment", "finalize", "reject"}


def attribution_header(role: str, model: str | None = None) -> str:
    """Return the one-line cheaphelp attribution header for a posted message.

    Identifies the message as machine-generated and names the agent (and model,
    when one backs the role). The harness adds this; agents never write it.
    """
    header = f"{ATTRIBUTION_PREFIX} · agent `{role}`"
    if model:
        header += f" · model `{model}`"
    return header


def with_attribution(body: str, *, role: str, model: str | None = None) -> str:
    """Prefix a message body with the visible attribution header + hidden marker."""
    return f"{attribution_header(role, model)}\n{BOT_MARKER}\n\n{body.strip()}\n"


def cheaphelp_message(body: str, role: str, config: Config) -> str:
    """Build a GitHub message body carrying cheaphelp's attribution header.

    Looks up the role's model (and variant) from `config` so the header records
    exactly what ran. Roles not backed by a model (e.g. the quality gate) omit it.
    """
    model = config.models.get(role)
    if model:
        variant = config.variant_for(role)
        model = f"{model} ({variant})" if variant else model
    return with_attribution(body, role=role, model=model)


def is_bot_comment(comment: Comment) -> bool:
    """Whether a comment was authored by the responder."""
    return BOT_MARKER in comment.body


def needs_turn(issue: Issue, comments: list[Comment], config: Config) -> bool:
    """Decide whether an issue is waiting on a responder turn.

    True when the issue is open, not already finalized/rejected, and the most
    recent activity came from a human (a new issue, or a human reply to us).
    """
    labels = set(issue.labels)
    done_labels = {config.labels[key] for key in RESPONDER_DONE_LABEL_KEYS}
    if labels & done_labels:
        return False
    if not comments:
        # Freshly opened issue with no replies yet.
        return True
    last = comments[-1]
    # If we spoke last, we are waiting on the human.
    return not is_bot_comment(last)


def build_prompt(issue: Issue, comments: list[Comment], *, conventions: str = "") -> str:
    """Render the conversation into the user message handed to the agent."""
    lines = [
        f"# Issue #{issue.number}: {issue.title}",
        "",
        f"Opened by @{issue.user}.",
        "",
        "## Issue body",
        "",
        issue.body or "_(no description provided)_",
        "",
        "## Conversation so far",
        "",
    ]
    if not comments:
        lines.append("_(no comments yet)_")
    else:
        for comment in comments:
            who = "responder (you)" if is_bot_comment(comment) else f"@{comment.user}"
            # Strip the hidden marker and the harness-added attribution header line;
            # who-said-what is already labelled, so they would just be noise here.
            stripped = comment.body.replace(BOT_MARKER, "")
            body = "\n".join(line for line in stripped.splitlines() if not line.startswith(ATTRIBUTION_PREFIX)).strip()
            lines.append(f"### {who}")
            lines.append("")
            lines.append(body)
            lines.append("")
    if conventions.strip():
        lines += [
            "",
            "## Repository conventions",
            "",
            conventions.rstrip(),
        ]
    lines.extend(
        [
            "---",
            "",
            "Read the repository in your working directory, then respond following "
            "your output protocol. End with the required json block.",
        ],
    )
    return "\n".join(lines)


@dataclass
class AppliedDecision:
    """Record of what the responder did with an issue, for logging."""

    number: int
    action: str
    posted_comment: bool = False
    labeled_ready: bool = False
    labeled_rejected: bool = False
    issue_md_path: str | None = None
    error: str | None = None


def write_issue_md(workspace: Workspace, owner: str, repo: str, number: int, content: str) -> Path:
    """Persist a finalized issues.md into the workspace state tree."""
    target_dir = workspace.state_dir / f"{owner}__{repo}" / f"issue-{number}"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "issues.md"
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path


def record_state(workspace: Workspace, owner: str, repo: str, number: int, data: dict) -> None:
    """Write a small per-issue state file (last action, timestamp)."""
    path = workspace.issue_state_path(owner, repo, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def apply_decision(
    gh: GitHubClient,
    workspace: Workspace,
    config: Config,
    owner: str,
    repo: str,
    issue: Issue,
    decision: dict,
) -> AppliedDecision:
    """Apply a parsed responder decision to GitHub and the workspace."""
    action = str(decision.get("action", "")).strip().lower()
    result = AppliedDecision(number=issue.number, action=action)
    if action not in VALID_ACTIONS:
        result.error = f"invalid action: {action!r}"
        return result

    reply = str(decision.get("reply", "")).strip()
    if reply:
        gh.create_comment(owner, repo, issue.number, cheaphelp_message(reply, "responder", config))
        result.posted_comment = True

    if action == "finalize":
        issue_md = str(decision.get("issue_md", "")).strip()
        if issue_md:
            path = write_issue_md(workspace, owner, repo, issue.number, issue_md)
            result.issue_md_path = str(path)
        gh.ensure_label(
            owner,
            repo,
            config.labels["ready"],
            color="0e8a16",
            description="cheaphelp: scope finalized, ready to plan",
        )
        gh.add_labels(owner, repo, issue.number, [config.labels["ready"]])
        result.labeled_ready = True

    elif action == "reject":
        gh.ensure_label(
            owner,
            repo,
            config.labels["rejected"],
            color="b60205",
            description="cheaphelp: declined",
        )
        gh.add_labels(owner, repo, issue.number, [config.labels["rejected"]])
        result.labeled_rejected = True

    record_state(
        workspace,
        owner,
        repo,
        issue.number,
        {"action": action, "issue_md": result.issue_md_path},
    )
    return result
