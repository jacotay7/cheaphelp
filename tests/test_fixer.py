"""Tests for cheaphelp's fixer role."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal import fixer, gitutil, opencode
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.opencode import AgentResult, UsageData
from cheaphelp._internal.registry import RepoEntry


def _seed_spec(ws: Workspace, repo: RepoEntry, number: int) -> None:
    issue_dir = ws.issue_dir(repo.owner, repo.name, number)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "issues.md").write_text("# Spec\n\nDo the thing.\n", encoding="utf-8")


def test_build_prompt_includes_command_and_output_tail() -> None:
    prompt = fixer.build_prompt(
        "# Spec",
        "ruff check . && pytest",
        "E501 line too long\nFAILED test_x\n",
        conventions="Use tabs.",
    )
    assert "ruff check . && pytest" in prompt
    assert "FAILED test_x" in prompt
    assert "Repository conventions" in prompt
    assert "Use tabs." in prompt


def test_build_prompt_truncates_long_output() -> None:
    big = "x" * 20_000
    prompt = fixer.build_prompt("# Spec", "pytest", big)
    assert "output truncated" in prompt
    # Only the tail is kept, not the whole 20k (a couple of stray "x" chars in
    # the surrounding boilerplate are fine).
    assert prompt.count("x") <= fixer._MAX_OUTPUT_CHARS + 50


def test_run_fix_commits_and_pushes_on_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="o", name="r", checks="pytest")
    _seed_spec(ws, repo, 1)

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: AgentResult(
            returncode=0,
            stdout="",
            stderr="",
            decision={"status": "done", "summary": "fixed"},
            usage=UsageData(cost_usd=0.01),
        ),
    )
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: True)
    pushed: list[str] = []
    monkeypatch.setattr(gitutil, "push_branch", lambda *_a, **k: pushed.append(k.get("branch", "")))

    res = fixer.run_fix(ws, Config(), repo, 1, "boom", tmp_path, token="tok")

    assert res.status == "done"
    assert res.committed is True
    assert pushed == ["cheaphelp/issue-1"]
    assert res.usage is not None
    assert res.usage.cost_usd == 0.01


def test_run_fix_no_push_when_nothing_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="o", name="r", checks="pytest")
    _seed_spec(ws, repo, 1)

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: AgentResult(
            returncode=0,
            stdout="",
            stderr="",
            decision={"status": "blocked", "summary": "stuck"},
        ),
    )
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: False)
    pushed: list[str] = []
    monkeypatch.setattr(gitutil, "push_branch", lambda *_a, **k: pushed.append("x"))

    res = fixer.run_fix(ws, Config(), repo, 1, "boom", tmp_path, token="tok")

    assert res.status == "blocked"
    assert res.committed is False
    assert pushed == []


def test_run_fix_unparseable_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = Workspace(tmp_path)
    ws.ensure()
    repo = RepoEntry(owner="o", name="r", checks="pytest")
    _seed_spec(ws, repo, 1)

    monkeypatch.setattr(
        opencode,
        "run_agent",
        lambda *_a, **_k: AgentResult(returncode=0, stdout="prose", stderr="", decision=None),
    )
    monkeypatch.setattr(gitutil, "commit_all", lambda *_a, **_k: False)

    res = fixer.run_fix(ws, Config(), repo, 1, "boom", tmp_path, token="tok")

    assert res.status == "unparseable"
    assert res.error == "unparseable"
