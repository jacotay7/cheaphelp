"""Tests for cheaphelp's opencode harness wrapper."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from cheaphelp._internal import (
    opencode,
)
from cheaphelp._internal.config import Config, Workspace


# --- opencode --------------------------------------------------------------
def test_extract_decision_from_messy_output() -> None:
    out = 'noise\n```json\n{"action": "comment", "reply": "hi"}\n```\n'
    assert opencode.extract_decision(out) == {"action": "comment", "reply": "hi"}


def test_extract_decision_prefers_last_block() -> None:
    out = '```json\n{"action": "comment"}\n```\n```json\n{"action": "finalize"}\n```'
    decision = opencode.extract_decision(out)
    assert decision is not None
    assert decision["action"] == "finalize"


def test_extract_decision_none_when_absent() -> None:
    assert opencode.extract_decision("just prose, no json") is None


def test_extract_decision_handles_braces_in_string_values() -> None:
    """Braces inside JSON string values must not break parsing."""
    out = '```json\n{"action": "comment", "summary": "changed {foo, bar} in file.py"}\n```'
    decision = opencode.extract_decision(out)
    assert decision is not None
    assert decision == {"action": "comment", "summary": "changed {foo, bar} in file.py"}


def test_extract_decision_handles_nested_object() -> None:
    """Nested JSON structures are parsed correctly."""
    out = '```json\n{"data": {"x": 1, "y": [1, 2, 3]}}\n```'
    decision = opencode.extract_decision(out)
    assert decision is not None
    assert decision == {"data": {"x": 1, "y": [1, 2, 3]}}


def test_extract_decision_handles_escaped_quotes() -> None:
    """Escaped quotes inside JSON string values round-trip intact."""
    out = '```json\n{"key": "value with \\"escaped\\" quote"}\n```'
    decision = opencode.extract_decision(out)
    assert decision is not None
    assert decision == {"key": 'value with "escaped" quote'}


def test_extract_decision_prefers_last_block_with_nested_braces() -> None:
    """When multiple ```json blocks exist, the last valid one wins, even with nested braces."""
    out = (
        '```json\n{"action": "first", "note": "plain"}\n```\n'
        "more text\n"
        '```json\n{"action": "second", "summary": "changed {foo, bar}"}\n```'
    )
    decision = opencode.extract_decision(out)
    assert decision is not None
    assert decision == {"action": "second", "summary": "changed {foo, bar}"}


def test_extract_decision_ignores_unclosed_fence() -> None:
    """A ```json fence that never reaches a balanced close returns None."""
    out = 'some text\n```json\n{"action": "broken"'
    decision = opencode.extract_decision(out)
    assert decision is None


def test_run_agent_reprompts_once_on_empty_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415 - local to keep the module import list lean

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        # First reply has no parseable json block; the retry returns a valid one.
        stdout = "thinking out loud, no json" if len(calls) == 1 else '```json\n{"status": "done"}\n```'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 2})
    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 2  # re-prompted exactly once
    assert opencode._REPROMPT_SUFFIX in calls[1][-1]  # the reminder rode along on the LAST attempt


def test_run_agent_retries_on_unparseable_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean exit with no parseable decision retries with exponential backoff."""
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="thinking out loud, no json", stderr="")

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    monkeypatch.setattr(opencode.time, "sleep", fake_sleep)

    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 3})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.decision is None
    assert result.returncode == 0
    assert result.ok is True
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert opencode._REPROMPT_SUFFIX not in calls[0][-1]
    assert opencode._REPROMPT_SUFFIX in calls[-1][-1]
    # Attempt 1: base * 2^(0) = 0.01, jitter ±0.25 → [0.0075, 0.0125]
    assert 0.0075 <= sleeps[0] <= 0.0125
    # Attempt 2: base * 2^(1) = 0.02, jitter ±0.25 → [0.015, 0.025]
    assert 0.015 <= sleeps[1] <= 0.025


def test_run_agent_unparseable_recovers_within_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After one unparseable attempt, the second produces valid JSON; the third must not run."""
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout="thinking out loud, no json", stderr="")
        # 2nd call returns valid JSON.
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 3})
    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 2
    # The 2nd call is NOT the last attempt (retry_attempts=3), so no suffix.
    assert opencode._REPROMPT_SUFFIX not in calls[1][-1]


def test_run_agent_exhausted_unparseable_returns_last_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All attempts produce unparseable output; the final result with reprrompt is returned."""
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="always just prose", stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 2})
    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert len(calls) == 2
    assert opencode._REPROMPT_SUFFIX in calls[-1][-1]
    assert result.decision is None
    assert result.returncode == 0
    assert result.ok is True


def test_run_agent_no_reprompt_when_decision_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 1  # no retry needed


def test_run_agent_retries_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        if len(calls) <= 2:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 3


def test_run_agent_retries_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        if len(calls) <= 2:
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=1.0)
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 3


def test_run_agent_exhausted_retries_returns_last_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=2, stdout="", stderr="no good")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 3})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.returncode == 2
    assert result.ok is False
    assert len(calls) == 3


def test_run_agent_exhausted_timeout_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=1.0)

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    cfg = Config.from_dict({"retry_base_delay": 0.01, "retry_attempts": 3})

    with pytest.raises(subprocess.TimeoutExpired):
        opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert len(calls) == 3


def test_run_agent_no_retry_on_clean_exit_with_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='```json\n{"status": "done"}\n```', stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 1  # no retry, no re-prompt


def test_run_agent_reprompt_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        stdout = "thinking out loud, no json" if len(calls) == 1 else '```json\n{"status": "done"}\n```'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 2  # re-prompt, not retry loop


def test_run_agent_backoff_is_exponential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    random.seed(42)  # deterministic jitter
    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)
    monkeypatch.setattr(opencode.time, "sleep", fake_sleep)
    cfg = Config.from_dict({"retry_base_delay": 1.0, "retry_attempts": 3})

    result = opencode.run_agent(ws, cfg, "worker", "do it", cwd=tmp_path)
    assert result.returncode == 1
    assert len(calls) == 3
    assert len(sleeps) == 2

    # Attempt 1: base * 2^(0) = 1.0, jitter ±0.25
    assert 0.75 <= sleeps[0] <= 1.25
    # Attempt 2: base * 2^(1) = 2.0, jitter ±0.5
    assert 1.5 <= sleeps[1] <= 2.5


# --- _save_unparsed_output -------------------------------------------------
def test_save_unparsed_output_writes_log_file(tmp_path: Path) -> None:
    """When the result has a clean exit and no decision, the log file is written."""
    issue_dir = tmp_path / "issue-1"
    result = opencode.AgentResult(returncode=0, stdout="hello\n", stderr="oops\n", decision=None)
    opencode._save_unparsed_output(issue_dir, "worker", result)

    log_path = issue_dir / "last_unparsed_worker.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "--- stdout ---" in content
    assert "hello" in content
    assert "--- stderr ---" in content
    assert "oops" in content


def test_save_unparsed_output_truncates_combined_to_64kb(tmp_path: Path) -> None:
    """Output exceeding 64 KB is truncated to the last 65536 bytes."""
    issue_dir = tmp_path / "issue-1"
    result = opencode.AgentResult(returncode=0, stdout="x" * 200_000, stderr="", decision=None)
    opencode._save_unparsed_output(issue_dir, "worker", result)

    log_path = issue_dir / "last_unparsed_worker.log"
    assert log_path.exists()
    size = log_path.stat().st_size
    assert size <= 70_000
    content = log_path.read_text(encoding="utf-8")
    assert "truncated to last" in content


def test_save_unparsed_output_noop_when_issue_dir_none(tmp_path: Path) -> None:
    """When issue_dir is None, no file is written."""
    result = opencode.AgentResult(returncode=0, stdout="hello\n", stderr="oops\n", decision=None)
    opencode._save_unparsed_output(None, "worker", result)

    # No file should exist anywhere under tmp_path.
    files = list(tmp_path.rglob("*"))
    assert not any(f.name.startswith("last_unparsed_") for f in files)


def test_save_unparsed_output_noop_when_decision_present(tmp_path: Path) -> None:
    """When decision is not None, no file is written."""
    issue_dir = tmp_path / "issue-1"
    result = opencode.AgentResult(returncode=0, stdout="hello\n", stderr="", decision={})
    opencode._save_unparsed_output(issue_dir, "worker", result)

    log_path = issue_dir / "last_unparsed_worker.log"
    assert not log_path.exists()


def test_save_unparsed_output_noop_when_nonzero_exit(tmp_path: Path) -> None:
    """When returncode != 0, no file is written even if decision is None."""
    issue_dir = tmp_path / "issue-1"
    result = opencode.AgentResult(returncode=1, stdout="boom", stderr="", decision=None)
    opencode._save_unparsed_output(issue_dir, "worker", result)

    log_path = issue_dir / "last_unparsed_worker.log"
    assert not log_path.exists()


# --- UsageData --------------------------------------------------------------
def test_usage_data_defaults() -> None:
    u = opencode.UsageData()
    assert u.prompt_tokens == 0
    assert u.completion_tokens == 0
    assert u.total_tokens == 0
    assert u.cost_usd == 0.0


def test_usage_data_addition() -> None:
    a = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    b = opencode.UsageData(prompt_tokens=100, completion_tokens=200, total_tokens=300, cost_usd=0.01)
    c = a + b
    assert c.prompt_tokens == 110
    assert c.completion_tokens == 220
    assert c.total_tokens == 330
    assert c.cost_usd == 0.011
    # Original objects unchanged.
    assert a.prompt_tokens == 10
    assert b.prompt_tokens == 100


def test_usage_data_iadd() -> None:
    a = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    b = opencode.UsageData(prompt_tokens=100, completion_tokens=200, total_tokens=300, cost_usd=0.01)
    result = a.__iadd__(b)
    assert result is a  # returns self
    assert a.prompt_tokens == 110
    assert a.completion_tokens == 220
    assert a.total_tokens == 330
    assert a.cost_usd == 0.011


def test_usage_data_to_dict_roundtrip() -> None:
    u = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    d = u.to_dict()
    assert d == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost_usd": 0.001}
    restored = opencode.UsageData.from_dict(d)
    assert restored == u


def test_usage_data_from_dict_accepts_cost_key() -> None:
    # OpenRouter returns "cost" in the usage payload; accept it as cost_usd.
    u = opencode.UsageData.from_dict({"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.005})
    assert u.prompt_tokens == 10
    assert u.completion_tokens == 20
    assert u.cost_usd == 0.005
    # cost_usd key takes precedence over cost when both are present.
    u2 = opencode.UsageData.from_dict({"prompt_tokens": 1, "cost": 0.01, "cost_usd": 0.02})
    assert u2.cost_usd == 0.02


def test_usage_data_from_dict_coerces_types() -> None:
    u = opencode.UsageData.from_dict({"prompt_tokens": "10", "completion_tokens": "20", "cost": "0.005"})
    assert u.prompt_tokens == 10
    assert u.completion_tokens == 20
    assert u.cost_usd == 0.005


def test_usage_data_from_dict_empty() -> None:
    u = opencode.UsageData.from_dict({})
    assert u.prompt_tokens == 0
    assert u.completion_tokens == 0
    assert u.total_tokens == 0
    assert u.cost_usd == 0.0


# --- _parse_usage -----------------------------------------------------------
def test_parse_usage_from_stderr() -> None:
    usage = opencode._parse_usage("", '{"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.001}')
    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 20
    assert usage.cost_usd == 0.001


def test_parse_usage_from_stdout_when_stderr_empty() -> None:
    usage = opencode._parse_usage(
        '{"prompt_tokens": 5, "completion_tokens": 15, "cost_usd": 0.002}',
        "",
    )
    assert usage is not None
    assert usage.prompt_tokens == 5
    assert usage.completion_tokens == 15
    assert usage.cost_usd == 0.002


def test_parse_usage_nested_usage_key() -> None:
    payload = '{"id": "xyz", "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01}}'
    usage = opencode._parse_usage("", payload)
    assert usage is not None
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 50
    assert usage.cost_usd == 0.01


def test_parse_usage_line_by_line_fallback() -> None:
    # A mixed stderr with a JSON usage object on one line.
    stderr = 'some log info\n{"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.0005}\ndone'
    usage = opencode._parse_usage("", stderr)
    assert usage is not None
    assert usage.prompt_tokens == 7
    assert usage.completion_tokens == 3
    assert usage.cost_usd == 0.0005


def test_parse_usage_stderr_takes_precedence() -> None:
    usage = opencode._parse_usage(
        '{"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}',
        '{"prompt_tokens": 99, "completion_tokens": 99, "cost": 0.099}',
    )
    assert usage is not None
    assert usage.prompt_tokens == 99  # stderr won
    assert usage.completion_tokens == 99


def test_parse_usage_returns_none_when_no_json() -> None:
    assert opencode._parse_usage("just prose", "") is None
    assert opencode._parse_usage("", "more prose") is None
    assert opencode._parse_usage("", "") is None
    assert opencode._parse_usage('noise\n```json\n{"status": "ok"}\n```', "") is None


# --- run_agent usage population ---------------------------------------------
def test_run_agent_populates_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    ws = Workspace(tmp_path)
    ws.ensure()
    monkeypatch.setattr(opencode, "find_opencode", lambda _cfg: Path("opencode"))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        stderr = '{"prompt_tokens": 50, "completion_tokens": 30, "cost": 0.004}'
        return SimpleNamespace(
            returncode=0,
            stdout='```json\n{"status": "done"}\n```',
            stderr=stderr,
        )

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path)
    assert result.decision == {"status": "done"}
    assert len(calls) == 1
    assert result.usage is not None
    assert result.usage.prompt_tokens == 50
    assert result.usage.completion_tokens == 30
    assert result.usage.cost_usd == 0.004


# --- run_agent unparseable log via mock ------------------------------------
@pytest.mark.parametrize("role", ["worker", "planner", "responder", "reviewer", "rework", "fixer"])
def test_run_agent_writes_unparsed_log_via_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """When CHEAPHELP_AGENT_MOCK points to unparseable prose, a log file is written."""
    mock_file = tmp_path / "mock.txt"
    mock_content = "This is just prose with no json block whatsoever."
    mock_file.write_text(mock_content, encoding="utf-8")
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", str(mock_file))

    ws = Workspace(tmp_path)
    ws.ensure()
    issue_dir = ws.issue_dir("o", "r", 1)

    result = opencode.run_agent(ws, Config(), role, "do it", cwd=tmp_path, issue_dir=issue_dir)

    assert result.decision is None
    log_path = issue_dir / f"last_unparsed_{role}.log"
    assert log_path.exists()
    assert mock_content in log_path.read_text(encoding="utf-8")


def test_run_agent_truncates_unparsed_log_via_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mock output is very large, the on-disk log is truncated to ~64 KB."""
    mock_file = tmp_path / "mock_large.txt"
    large_content = "x" * 200_000
    mock_file.write_text(large_content, encoding="utf-8")
    monkeypatch.setenv("CHEAPHELP_AGENT_MOCK", str(mock_file))

    ws = Workspace(tmp_path)
    ws.ensure()
    issue_dir = ws.issue_dir("o", "r", 1)

    result = opencode.run_agent(ws, Config(), "worker", "do it", cwd=tmp_path, issue_dir=issue_dir)

    assert result.decision is None
    log_path = issue_dir / "last_unparsed_worker.log"
    assert log_path.exists()
    assert log_path.stat().st_size <= 70_000
    content = log_path.read_text(encoding="utf-8")
    assert "truncated to last" in content


# --- opencode config shape --------------------------------------------------
def test_build_opencode_config_shape() -> None:
    doc = opencode.build_opencode_config(Config())
    assert doc["$schema"] == opencode.OPENCODE_SCHEMA
    assert set(doc["agent"]) == {"responder", "planner", "worker", "reviewer", "rework", "fixer"}
    # Responder is read-only; worker and fixer can write.
    assert doc["agent"]["responder"]["tools"]["edit"] is False
    assert doc["agent"]["worker"]["tools"]["edit"] is True
    assert doc["agent"]["fixer"]["tools"]["edit"] is True
    # OpenRouter provider lists models without the opencode prefix.
    assert "deepseek/deepseek-v4-flash" in doc["provider"]["openrouter"]["models"]
    assert "minimax/minimax-m3" in doc["provider"]["openrouter"]["models"]


def test_sandbox_permissions_default_on() -> None:
    doc = opencode.build_opencode_config(Config())
    worker = doc["agent"]["worker"]["permission"]
    reader = doc["agent"]["planner"]["permission"]
    # Confined to the working directory, no network tools.
    assert worker["external_directory"] == "deny"
    assert reader["external_directory"] == "deny"
    assert reader["webfetch"] == "deny"
    # Worker: allow-by-default bash but dangerous commands denied; readers deny-default.
    assert worker["bash"]["*"] == "allow"
    assert worker["bash"]["sudo*"] == "deny"
    assert worker["bash"]["git push*"] == "deny"
    assert reader["bash"]["*"] == "deny"
    assert reader["bash"]["git status*"] == "allow"
    # Readers cannot edit; worker can.
    assert reader["edit"] == "deny"
    assert worker["edit"] == "allow"


def test_sandbox_can_be_disabled() -> None:
    cfg = Config.from_dict(
        {"sandbox": {"confine_to_workdir": False, "restrict_bash": False, "no_network_tools": False}},
    )
    perm = opencode.build_opencode_config(cfg)["agent"]["worker"]["permission"]
    assert perm["bash"] == "allow"
    assert "external_directory" not in perm
    assert "webfetch" not in perm
