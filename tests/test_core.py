"""Tests for cheaphelp's core logic that does not require network access."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal import opencode, planner, systemd, worker
from cheaphelp._internal.config import DEFAULT_MODELS, Config, Workspace
from cheaphelp._internal.env import parse_env, read_env_file, update_env_file
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.orchestrator import classify
from cheaphelp._internal.registry import Registry, RepoEntry, parse_slug
from cheaphelp._internal.responder import BOT_MARKER, build_prompt, needs_turn
from cheaphelp._internal.tasks import DONE, TaskStore


# --- config / workspace ----------------------------------------------------
def test_workspace_roundtrip(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    assert not ws.exists()
    ws.ensure()
    ws.save_config(Config())
    assert ws.exists()
    loaded = ws.load_config()
    assert loaded.models == DEFAULT_MODELS
    assert loaded.model_for("responder") == DEFAULT_MODELS["responder"]


def test_config_merges_defaults() -> None:
    cfg = Config.from_dict({"models": {"worker": "openrouter/custom"}})
    assert cfg.model_for("worker") == "openrouter/custom"
    # Missing roles fall back to defaults.
    assert cfg.model_for("responder") == DEFAULT_MODELS["responder"]


def test_variant_for() -> None:
    cfg = Config()
    assert cfg.variant_for("responder") == "max"  # default cheap-but-strong tier
    assert cfg.variant_for("planner") == ""  # provider default
    # Round-trips through serialization.
    assert Config.from_dict(cfg.to_dict()).variant_for("responder") == "max"
    # Override.
    assert Config.from_dict({"variants": {"planner": "high"}}).variant_for("planner") == "high"


# --- env -------------------------------------------------------------------
def test_parse_env_handles_quotes_and_comments() -> None:
    parsed = parse_env('# comment\nexport A="hello"\nB=plain\n\nC=\n')
    assert parsed == {"A": "hello", "B": "plain", "C": ""}


def test_update_env_file_preserves_and_chmods(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    update_env_file(path, {"GITHUB_TOKEN": "abc"})
    update_env_file(path, {"OPENROUTER_API_KEY": "xyz"})
    values = read_env_file(path)
    assert values["GITHUB_TOKEN"] == "abc"
    assert values["OPENROUTER_API_KEY"] == "xyz"
    # Empty updates must not clobber existing secrets.
    update_env_file(path, {"GITHUB_TOKEN": ""})
    assert read_env_file(path)["GITHUB_TOKEN"] == "abc"
    assert (path.stat().st_mode & 0o077) == 0  # owner-only permissions


# --- registry --------------------------------------------------------------
def test_parse_slug_variants() -> None:
    assert parse_slug("octocat/Hello-World") == ("octocat", "Hello-World")
    assert parse_slug("https://github.com/octocat/Hello-World.git") == ("octocat", "Hello-World")
    with pytest.raises(ValueError, match="Invalid repository slug"):
        parse_slug("not-a-slug")


def test_registry_add_remove_toggle(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    assert reg.add(RepoEntry(owner="o", name="r")) is True
    assert reg.add(RepoEntry(owner="o", name="r")) is False  # duplicate
    assert reg.set_enabled("o", "r", enabled=False) is True
    assert reg.find("o", "r").enabled is False
    assert reg.remove("o", "r") is True
    assert reg.remove("o", "r") is False


def test_registry_checks_roundtrip(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    assert RepoEntry(owner="o", name="r").checks == ""  # default: gate disabled
    reg.add(RepoEntry(owner="o", name="r", checks="ruff check . && pytest"))
    assert reg.find("o", "r").checks == "ruff check . && pytest"


# --- responder -------------------------------------------------------------
def _issue(number: int = 1, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title="t",
        body="b",
        state="open",
        labels=labels or [],
        user="alice",
        html_url="",
    )


def _comment(body: str, user: str) -> Comment:
    return Comment(id=1, body=body, user=user, created_at="")


def test_needs_turn_logic() -> None:
    cfg = Config()
    bot = "mybot"
    assert needs_turn(_issue(), [], bot, cfg) is True  # fresh issue
    bot_c = _comment(BOT_MARKER + "\nQ?", bot)
    assert needs_turn(_issue(), [bot_c], bot, cfg) is False  # waiting on human
    human_c = _comment("answer", "alice")
    assert needs_turn(_issue(), [bot_c, human_c], bot, cfg) is True  # human replied
    ready = _issue(labels=[cfg.labels["ready"]])
    assert needs_turn(ready, [human_c], bot, cfg) is False  # already finalized


def test_build_prompt_includes_thread() -> None:
    prompt = build_prompt(_issue(number=42), [_comment("hi", "alice")], "mybot")
    assert "Issue #42" in prompt
    assert "@alice" in prompt
    assert "hi" in prompt


# --- opencode --------------------------------------------------------------
def test_extract_decision_from_messy_output() -> None:
    out = 'noise\n```json\n{"action": "comment", "reply": "hi"}\n```\n'
    assert opencode.extract_decision(out) == {"action": "comment", "reply": "hi"}


def test_extract_decision_prefers_last_block() -> None:
    out = '```json\n{"action": "comment"}\n```\n```json\n{"action": "finalize"}\n```'
    assert opencode.extract_decision(out)["action"] == "finalize"


def test_extract_decision_none_when_absent() -> None:
    assert opencode.extract_decision("just prose, no json") is None


def test_build_opencode_config_shape() -> None:
    doc = opencode.build_opencode_config(Config())
    assert doc["$schema"] == opencode.OPENCODE_SCHEMA
    assert set(doc["agent"]) == {"responder", "planner", "worker", "reviewer"}
    # Responder is read-only; worker can write.
    assert doc["agent"]["responder"]["tools"]["edit"] is False
    assert doc["agent"]["worker"]["tools"]["edit"] is True
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


# --- systemd ---------------------------------------------------------------
def test_normalize_interval() -> None:
    assert systemd.normalize_interval("10m") == "10min"
    assert systemd.normalize_interval("2h") == "2h"
    assert systemd.normalize_interval("30s") == "30s"
    with pytest.raises(ValueError, match="Invalid interval"):
        systemd.normalize_interval("soon")


def test_render_units_contains_exec_and_interval(tmp_path: Path) -> None:
    units = systemd.render_units(home=tmp_path, interval="15m")
    assert "OnUnitActiveSec=15min" in units.timer
    assert "run --once" in units.service
    assert f"CHEAPHELP_HOME={tmp_path}" in units.service


# --- planner manifest ------------------------------------------------------
def test_parse_manifest_valid() -> None:
    summary, tasks = planner.parse_manifest(
        {
            "plan_summary": "do the thing",
            "tasks": [
                {"id": "t1", "title": "first", "depends_on": []},
                {"id": "t2", "title": "second", "depends_on": ["t1"]},
            ],
        },
    )
    assert summary == "do the thing"
    assert [t.id for t in tasks] == ["t1", "t2"]


@pytest.mark.parametrize(
    "decision",
    [
        {"tasks": []},  # no tasks
        {"tasks": [{"id": "t1"}]},  # missing title
        {"tasks": [{"id": "t1", "title": "a"}, {"id": "t1", "title": "b"}]},  # dup id
        {"tasks": [{"id": "t1", "title": "a", "depends_on": ["tX"]}]},  # bad dep
    ],
)
def test_parse_manifest_rejects_bad(decision: dict) -> None:
    with pytest.raises(ValueError, match=r"task|depend"):
        planner.parse_manifest(decision)


# --- task store ------------------------------------------------------------
def test_task_store_lifecycle(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "issue-1")
    _, tasks = planner.parse_manifest(
        {
            "tasks": [
                {"id": "t1", "title": "one", "depends_on": []},
                {"id": "t2", "title": "two", "depends_on": ["t1"]},
            ],
        },
    )
    store.materialize(tasks)
    assert (tmp_path / "issue-1" / "tasks" / "t1.task.md").exists()

    # Only t1 is ready (t2 depends on it).
    assert store.next_ready().id == "t1"
    assert not store.all_done()

    store.set_status("t1", DONE, summary="did one")
    assert store.next_ready().id == "t2"
    store.set_status("t2", DONE)
    assert store.all_done()
    assert store.next_ready() is None


def test_task_store_blocked(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "issue-2")
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "x"}]})
    store.materialize(tasks)
    store.set_status("t1", "blocked")
    assert store.is_blocked()
    assert not store.all_done()


def test_worker_branch_name() -> None:
    assert worker.branch_name(42) == "cheaphelp/issue-42"


# --- orchestrator stage classification ------------------------------------
def _issue_with(labels: list[str]) -> Issue:
    return Issue(number=1, title="t", body="b", state="open", labels=labels, user="u", html_url="")


def test_classify_stages() -> None:
    cfg = Config()
    lab = cfg.labels
    bot = "bot"
    # Fresh issue, human opened it -> responder.
    assert classify(_issue_with([]), [], bot, cfg) == "responder"
    # Ready / needs-replan -> planner.
    assert classify(_issue_with([lab["ready"]]), [], bot, cfg) == "planner"
    assert classify(_issue_with([lab["needs_replan"]]), [], bot, cfg) == "planner"
    # Planned -> build.
    assert classify(_issue_with([lab["planned"]]), [], bot, cfg) == "build"
    # Terminal / waiting -> nothing.
    assert classify(_issue_with([lab["rejected"]]), [], bot, cfg) is None
    assert classify(_issue_with([lab["in_review"]]), [], bot, cfg) is None
    assert classify(_issue_with([lab["needs_human"]]), [], bot, cfg) is None
