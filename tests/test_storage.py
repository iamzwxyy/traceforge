from __future__ import annotations

import sqlite3
from pathlib import Path

from traceforge.config import Settings
from traceforge.models import EventType, RunRecord, RunState
from traceforge.storage import SnapshotRecord, Storage


def test_run_and_events_round_trip(storage: Storage, settings: Settings) -> None:
    workspace = settings.workspace
    run = RunRecord(id="run-1", task="Fix it", workspace=str(workspace))
    storage.create_run(run)
    run.state = RunState.PLANNING
    run.messages.append({"role": "user", "content": "Fix it"})
    storage.save_run(run)

    event = storage.append_event("run-1", EventType.STATE_CHANGED, {"state": "planning"})
    loaded = storage.get_run("run-1")

    assert loaded.state is RunState.PLANNING
    assert loaded.messages[0]["content"] == "Fix it"
    assert event.seq == 1
    assert storage.get_events("run-1")[0].payload == {"state": "planning"}


def test_snapshot_is_write_once(storage: Storage, tmp_path: Path) -> None:
    run = RunRecord(id="run-1", task="Fix it", workspace=str(tmp_path))
    storage.create_run(run)
    first = SnapshotRecord("run-1", "a.txt", True, b"a", 0o644, "one", "one")
    second = SnapshotRecord("run-1", "a.txt", True, b"b", 0o600, "two", "two")

    assert storage.save_snapshot_if_absent(first)
    assert not storage.save_snapshot_if_absent(second)
    assert storage.list_snapshots("run-1")[0].content == b"a"


def test_mark_active_runs_interrupted(storage: Storage, settings: Settings) -> None:
    workspace = settings.workspace
    storage.create_run(
        RunRecord(
            id="run-active",
            task="Fix it",
            workspace=str(workspace),
            state=RunState.EXECUTING,
        )
    )
    storage.create_run(
        RunRecord(id="run-done", task="Done", workspace=str(workspace), state=RunState.SUCCEEDED)
    )
    storage.create_run(
        RunRecord(
            id="run-interrupted",
            task="Already stopped",
            workspace=str(workspace),
            state=RunState.INTERRUPTED,
            interrupted_from=RunState.EXECUTING,
        )
    )

    assert storage.mark_active_runs_interrupted(workspace) == 1
    assert storage.get_run("run-active").state is RunState.INTERRUPTED
    assert storage.get_run("run-done").state is RunState.SUCCEEDED
    assert storage.get_run("run-interrupted").interrupted_from is RunState.EXECUTING


def test_storage_migrates_legacy_run_columns(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, task TEXT NOT NULL, workspace TEXT NOT NULL,
            state TEXT NOT NULL, verifier_enabled INTEGER NOT NULL, plan_json TEXT,
            clarification_json TEXT, pending_approval_json TEXT, verification_json TEXT,
            messages_json TEXT NOT NULL DEFAULT '[]', step_count INTEGER NOT NULL DEFAULT 0,
            repair_cycles INTEGER NOT NULL DEFAULT 0, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    migrated = Storage(database)
    try:
        migrated.create_run(RunRecord(id="new", task="Task", workspace=str(tmp_path)))
        loaded = migrated.get_run("new")
        assert loaded.plan_approved is False
        assert loaded.interrupted_from is None
    finally:
        migrated.close()
