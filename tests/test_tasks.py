"""Tests for cheaphelp's task manifest and per-issue task/cost stores."""

from __future__ import annotations

import json
from pathlib import Path

from cheaphelp._internal import (
    opencode,
    planner,
)
from cheaphelp._internal.tasks import BLOCKED, DONE, PENDING, IssueCostStore, TaskStore


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


def test_task_store_record_attempt(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "issue-3")
    _, tasks = planner.parse_manifest({"tasks": [{"id": "t1", "title": "x"}]})
    store.materialize(tasks)
    assert store.load()[0].attempts == 0
    assert store.record_attempt("t1") == 1
    assert store.record_attempt("t1") == 2
    assert store.load()[0].attempts == 2


def test_task_store_reset_all(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "issue-4")
    _, tasks = planner.parse_manifest(
        {
            "tasks": [
                {"id": "t1", "title": "pending with attempts", "status": PENDING},
                {"id": "t2", "title": "blocked", "status": BLOCKED},
                {"id": "t3", "title": "done", "status": DONE},
            ],
        },
    )
    store.materialize(tasks)

    # Bump attempt counter on the PENDING task (t1) so it has attempts > 0.
    store.record_attempt("t1")
    store.record_attempt("t1")
    store.record_attempt("t1")

    # Give t3 a summary (as a DONE task would have).
    store.set_status("t3", DONE, summary="completed successfully")

    # --- reset ---------------------------------------------------------------
    updated = store.reset_all()

    # Every task has attempts == 0.
    for t in updated:
        assert t.attempts == 0, f"{t.id} still has attempts={t.attempts}"

    # The BLOCKED task (t2) is now PENDING.
    t2 = next(t for t in updated if t.id == "t2")
    assert t2.status == PENDING, f"t2 status is {t2.status}, expected PENDING"

    # The DONE task (t3) is still DONE and its summary is preserved.
    t3 = next(t for t in updated if t.id == "t3")
    assert t3.status == DONE
    assert t3.summary == "completed successfully"

    # The PENDING task (t1) that was not blocked remains PENDING.
    t1 = next(t for t in updated if t.id == "t1")
    assert t1.status == PENDING

    # --- persist check: re-load from disk -----------------------------------
    reloaded = store.load()
    assert len(reloaded) == 3
    for t in reloaded:
        assert t.attempts == 0, f"{t.id} still has attempts={t.attempts} on disk"
    assert next(t for t in reloaded if t.id == "t2").status == PENDING
    assert next(t for t in reloaded if t.id == "t3").status == DONE
    assert next(t for t in reloaded if t.id == "t3").summary == "completed successfully"


# --- issue cost store -------------------------------------------------------
def test_issue_cost_store_load_missing_file_returns_zeros(tmp_path: Path) -> None:
    store = IssueCostStore(tmp_path / "issue-1")
    usage = store.load()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.cost_usd == 0.0


def test_issue_cost_store_add_returns_cumulative_total(tmp_path: Path) -> None:
    store = IssueCostStore(tmp_path / "issue-2")
    u1 = opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001)
    total = store.add(u1)
    assert total.prompt_tokens == 10
    assert total.completion_tokens == 20
    assert total.total_tokens == 30
    assert total.cost_usd == 0.001
    # Verify the file was written with the right shape.
    assert (tmp_path / "issue-2" / "cost.json").exists()
    data = json.loads((tmp_path / "issue-2" / "cost.json").read_text(encoding="utf-8"))
    assert data == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost_usd": 0.001}


def test_issue_cost_store_accumulates_across_instances(tmp_path: Path) -> None:
    """Two add() calls across separate IssueCostStore instances simulate restart."""
    store1 = IssueCostStore(tmp_path / "issue-3")
    store1.add(opencode.UsageData(prompt_tokens=5, completion_tokens=5, total_tokens=10, cost_usd=0.0005))

    store2 = IssueCostStore(tmp_path / "issue-3")
    total = store2.add(opencode.UsageData(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.001))
    assert total.prompt_tokens == 15
    assert total.completion_tokens == 25
    assert total.total_tokens == 40
    assert total.cost_usd == 0.0015

    # Verify persistence: a third instance reads back the cumulative total.
    store3 = IssueCostStore(tmp_path / "issue-3")
    loaded = store3.load()
    assert loaded.prompt_tokens == 15
    assert loaded.completion_tokens == 25
    assert loaded.total_tokens == 40
    assert loaded.cost_usd == 0.0015


def test_issue_cost_store_corrupt_file_treated_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "issue-4" / "cost.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    store = IssueCostStore(tmp_path / "issue-4")
    usage = store.load()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.cost_usd == 0.0

    # Non-dict JSON is also treated as zero.
    path.write_text("[]", encoding="utf-8")
    usage = store.load()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.cost_usd == 0.0


def test_issue_cost_store_save_creates_parent_dir(tmp_path: Path) -> None:
    """save() creates the parent directory when it does not exist."""
    store = IssueCostStore(tmp_path / "a" / "b" / "issue-5")
    usage = opencode.UsageData(prompt_tokens=1, completion_tokens=2, total_tokens=3, cost_usd=0.0001)
    store.save(usage)
    assert store.path.exists()
    loaded = store.load()
    assert loaded.prompt_tokens == 1
    assert loaded.completion_tokens == 2
    assert loaded.total_tokens == 3
    assert loaded.cost_usd == 0.0001
