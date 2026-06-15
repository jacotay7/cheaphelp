"""Tests for cheaphelp's task manifest and per-issue task/cost stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    # Verify the file was written with the new schema shape.
    assert (tmp_path / "issue-2" / "cost.json").exists()
    data = json.loads((tmp_path / "issue-2" / "cost.json").read_text(encoding="utf-8"))
    assert data == {
        "total": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost_usd": 0.001},
        "by_role": {},
    }


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


def test_issue_cost_store_migrates_legacy_flat_format(tmp_path: Path) -> None:
    """A legacy flat UsageData dict (no 'total' key) is upgraded transparently."""
    cost_dir = tmp_path / "issue-legacy"
    cost_dir.mkdir(parents=True, exist_ok=True)
    legacy = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost_usd": 0.001}
    (cost_dir / "cost.json").write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    store = IssueCostStore(cost_dir)
    total = store.load()
    assert total.prompt_tokens == 10
    assert total.completion_tokens == 20
    assert total.total_tokens == 30
    assert total.cost_usd == 0.001

    # Per-role data is empty for migrated files.
    assert store.load_by_role() == {}
    assert store.load_role_counts() == {}


def test_issue_cost_store_add_with_role_accumulates_per_role(tmp_path: Path) -> None:
    """add(usage, role=...) accumulates per-role data alongside the total."""
    store = IssueCostStore(tmp_path / "issue-roles")

    u1 = opencode.UsageData(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.002)
    u2 = opencode.UsageData(prompt_tokens=20, completion_tokens=10, total_tokens=30, cost_usd=0.004)
    u3 = opencode.UsageData(prompt_tokens=5, completion_tokens=15, total_tokens=20, cost_usd=0.003)
    u4 = opencode.UsageData(prompt_tokens=8, completion_tokens=12, total_tokens=20, cost_usd=0.002)

    total1 = store.add(u1, role="responder")
    assert total1.prompt_tokens == 10

    total2 = store.add(u2, role="planner")
    assert total2.prompt_tokens == 30

    total3 = store.add(u3, role="worker")
    assert total3.prompt_tokens == 35

    total4 = store.add(u4, role="worker")
    assert total4.prompt_tokens == 43

    # Cumulative total = sum of all four.
    total = store.load()
    assert total.prompt_tokens == 43
    assert total.completion_tokens == 42
    assert total.total_tokens == 85
    assert total.cost_usd == pytest.approx(0.011)

    # Per-role breakdown.
    by_role = store.load_by_role()
    assert by_role["responder"].prompt_tokens == 10
    assert by_role["responder"].completion_tokens == 5
    assert by_role["planner"].prompt_tokens == 20
    assert by_role["planner"].completion_tokens == 10
    assert by_role["worker"].prompt_tokens == 13
    assert by_role["worker"].completion_tokens == 27

    # Role call counts.
    counts = store.load_role_counts()
    assert counts == {"responder": 1, "planner": 1, "worker": 2}


def test_issue_cost_store_missing_by_role_is_not_loaded(tmp_path: Path) -> None:
    """A file with 'total' but no 'by_role' loads cleanly."""
    cost_dir = tmp_path / "issue-no-byrole"
    cost_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "total": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "cost_usd": 0.0005},
    }
    (cost_dir / "cost.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    store = IssueCostStore(cost_dir)
    total = store.load()
    assert total.prompt_tokens == 5
    assert total.completion_tokens == 5
    assert total.total_tokens == 10
    assert total.cost_usd == 0.0005

    assert store.load_by_role() == {}
    assert store.load_role_counts() == {}


def test_issue_cost_store_corrupt_or_missing_file_yields_empty_by_role(tmp_path: Path) -> None:
    """Corrupt JSON or missing file: load_by_role and load_role_counts return {}."""
    # Missing file.
    store_missing = IssueCostStore(tmp_path / "issue-missing")
    assert store_missing.load_by_role() == {}
    assert store_missing.load_role_counts() == {}

    # Corrupt JSON.
    corrupt_dir = tmp_path / "issue-corrupt"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    (corrupt_dir / "cost.json").write_text("not json", encoding="utf-8")
    store_corrupt = IssueCostStore(corrupt_dir)
    assert store_corrupt.load_by_role() == {}
    assert store_corrupt.load_role_counts() == {}

    # Non-dict JSON.
    nd_dir = tmp_path / "issue-nondict"
    nd_dir.mkdir(parents=True, exist_ok=True)
    (nd_dir / "cost.json").write_text("[]", encoding="utf-8")
    store_nd = IssueCostStore(nd_dir)
    assert store_nd.load_by_role() == {}
    assert store_nd.load_role_counts() == {}
