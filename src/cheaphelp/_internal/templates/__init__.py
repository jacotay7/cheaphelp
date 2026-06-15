"""Bundled agent prompt templates, loaded as package data."""

from __future__ import annotations

from importlib import resources

# Agent roles that ship with cheaphelp. The "fixer" runs only when the
# deterministic quality gate fails after a build, repairing the working tree
# before the issue is sent back to the planner.
AGENT_ROLES = ("responder", "planner", "worker", "reviewer", "rework", "fixer")


def load_prompt(role: str) -> str:
    """Return the bundled prompt text for an agent role."""
    if role not in AGENT_ROLES:
        raise KeyError(f"Unknown agent role: {role!r}")
    # Use __name__ (always a str for this package's __init__) rather than
    # __package__ (typed str | None) so the type checker is satisfied.
    return resources.files(__name__).joinpath(f"{role}.md").read_text(encoding="utf-8")


def load_all_prompts() -> dict[str, str]:
    """Return all bundled agent prompts keyed by role."""
    return {role: load_prompt(role) for role in AGENT_ROLES}
