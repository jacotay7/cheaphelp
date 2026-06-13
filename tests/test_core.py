"""Tests for cheaphelp's core logic that does not require network access."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheaphelp._internal import opencode, planner, systemd, worker
from cheaphelp._internal.config import DEFAULT_AGENT_TIMEOUT, DEFAULT_MODELS, Config, Workspace
from cheaphelp._internal.env import parse_env, read_env_file, update_env_file
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.orchestrator import classify
from cheaphelp._internal.registry import Registry, RepoEntry, parse_slug
from cheaphelp._internal.responder import (
    ATTRIBUTION_PREFIX,
    BOT_MARKER,
    attribution_header,
    build_prompt,
    cheaphelp_message,
    is_bot_comment,
    needs_turn,
)
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


def test_agent_timeout_default_and_roundtrip() -> None:
    # Default when constructed with no args.
    assert Config().agent_timeout == DEFAULT_AGENT_TIMEOUT == 600.0
    # Default when the key is absent from the on-disk dict.
    assert Config.from_dict({}).agent_timeout == 600.0
    # User override is honoured by from_dict and preserved by to_dict.
    cfg = Config.from_dict({"agent_timeout": 1200})
    assert cfg.agent_timeout == 1200.0
    assert Config.from_dict(cfg.to_dict()).agent_timeout == 1200.0
    # Float-typed values are accepted (the spec says `float`).
    assert Config.from_dict({"agent_timeout": 30.5}).agent_timeout == 30.5


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
    found = reg.find("o", "r")
    assert found is not None
    assert found.enabled is False
    assert reg.remove("o", "r") is True
    assert reg.remove("o", "r") is False


def test_registry_checks_roundtrip(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    assert RepoEntry(owner="o", name="r").checks == ""  # default: gate disabled
    assert RepoEntry(owner="o", name="r").autofix == ""  # default: auto-fix disabled
    reg.add(RepoEntry(owner="o", name="r", checks="ruff check . && pytest", autofix="ruff format ."))
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "ruff check . && pytest"
    assert found.autofix == "ruff format ."


def test_registry_update(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "repos.json")
    reg.add(
        RepoEntry(
            owner="o",
            name="r",
            default_branch="develop",
            enabled=False,
            added_at="2024-01-01T00:00:00+00:00",
            checks="ruff check . && pytest",
            autofix="ruff format .",
        ),
    )

    # 1. Update only checks -> autofix is unchanged (and vice versa).
    assert reg.update("o", "r", checks="pytest -q") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "pytest -q"
    assert found.autofix == "ruff format ."

    assert reg.update("o", "r", autofix="ruff check --fix .") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "pytest -q"
    assert found.autofix == "ruff check --fix ."

    # 2. Update both checks and autofix in a single call.
    assert reg.update("o", "r", checks="make test", autofix="make format") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "make test"
    assert found.autofix == "make format"

    # 3. Setting checks="" (or autofix="") clears the value.
    assert reg.update("o", "r", checks="", autofix="") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == ""
    assert found.autofix == ""

    # 4. Updating a slug that isn't registered returns False and does not
    # create the file.
    missing_path = tmp_path / "missing.json"
    missing_reg = Registry(missing_path)
    assert missing_reg.update("unknown", "thing", checks="x") is False
    assert not missing_path.exists()

    # 5. update leaves enabled, default_branch, and added_at untouched when
    # called with only checks / autofix.
    assert reg.update("o", "r", checks="make lint", autofix="make fmt") is True
    found = reg.find("o", "r")
    assert found is not None
    assert found.enabled is False
    assert found.default_branch == "develop"
    assert found.added_at == "2024-01-01T00:00:00+00:00"

    # 6. Calling update with no kwargs (or only None kwargs) returns True,
    # does not change any field, and does not rewrite the file.
    mtime_before = reg.path.stat().st_mtime_ns
    assert reg.update("o", "r") is True
    assert reg.update("o", "r", checks=None, autofix=None) is True
    mtime_after = reg.path.stat().st_mtime_ns
    assert mtime_before == mtime_after
    found = reg.find("o", "r")
    assert found is not None
    assert found.checks == "make lint"
    assert found.autofix == "make fmt"


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


def test_attribution_header_names_agent_and_model() -> None:
    header = attribution_header("responder", "openrouter/x")
    assert header.startswith(ATTRIBUTION_PREFIX)
    assert "responder" in header
    assert "openrouter/x" in header
    # A role with no backing model omits the model segment.
    assert "model" not in attribution_header("quality-gate", None)


def test_cheaphelp_message_prefixes_header_marker_and_is_detected() -> None:
    cfg = Config()  # responder has a model + the "max" variant by default
    body = cheaphelp_message("hello world", "responder", cfg)
    assert body.startswith(ATTRIBUTION_PREFIX)
    assert cfg.model_for("responder") in body
    assert "(max)" in body  # variant recorded
    assert BOT_MARKER in body
    assert "hello world" in body
    # The hidden marker keeps the comment recognisable as cheaphelp's own.
    assert is_bot_comment(_comment(body, "someone-else"), "mybot") is True


def test_build_prompt_strips_attribution_header_from_thread() -> None:
    cfg = Config()
    own = cheaphelp_message("an earlier question", "responder", cfg)
    prompt = build_prompt(_issue(number=7), [_comment(own, "mybot")], "mybot")
    # The visible header and hidden marker are not shown back to the responder.
    assert ATTRIBUTION_PREFIX not in prompt
    assert BOT_MARKER not in prompt
    assert "an earlier question" in prompt


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


def test_parse_manifest_known_ids_allows_external_dep() -> None:
    # A corrective task may depend on an already-done task id from a prior round.
    _, tasks = planner.parse_manifest(
        {"tasks": [{"id": "fix1", "title": "fix", "depends_on": ["t2"]}]},
        known_ids={"t1", "t2"},
    )
    assert tasks[0].depends_on == ["t2"]


def test_merge_tasks_normal_case() -> None:
    done = [
        planner.Task(id="t1", title="one", status=DONE),
        planner.Task(id="t2", title="two", status=DONE),
    ]
    # Fresh corrective tasks (as the planner is instructed to produce) that
    # depend on already-done work.
    new = [
        planner.Task(id="fix1", title="fix the thing", depends_on=["t2"]),
        planner.Task(id="fix2", title="and another", depends_on=["fix1"]),
    ]
    merged = planner.merge_tasks(done, new)
    assert [t.id for t in merged] == ["t1", "t2", "fix1", "fix2"]
    assert all(t.status == DONE for t in merged[:2])  # done work preserved
    assert merged[2].depends_on == ["t2"]  # dep on done task kept
    assert merged[3].depends_on == ["fix1"]  # internal dep kept


def test_merge_tasks_renames_id_collision() -> None:
    done = [planner.Task(id="t1", title="one", status=DONE)]
    new = [planner.Task(id="t1", title="corrective")]  # reuses a done id
    merged = planner.merge_tasks(done, new)
    assert merged[0].id == "t1"  # done task untouched
    assert merged[1].id != "t1"  # new colliding task renamed
    assert merged[1].title == "corrective"


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
    ready = store.next_ready()
    assert ready is not None
    assert ready.id == "t1"
    assert not store.all_done()

    store.set_status("t1", DONE, summary="did one")
    ready = store.next_ready()
    assert ready is not None
    assert ready.id == "t2"
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


def test_worker_build_prompt_includes_gate_commands() -> None:
    task = worker.Task(id="t1", title="Do the thing")
    prompt = worker.build_prompt(
        task,
        "spec",
        autofix="ruff check --fix .",
        checks="ruff check . && pytest -q",
    )
    assert "Quality gate" in prompt
    assert "ruff check --fix ." in prompt
    assert "ruff check . && pytest -q" in prompt
    assert "Do not report `done`" in prompt


def test_worker_build_prompt_omits_gate_when_unset() -> None:
    prompt = worker.build_prompt(worker.Task(id="t1", title="Do the thing"), "spec")
    assert "Quality gate" not in prompt


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
