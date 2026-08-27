from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.models import EventType, ProjectRecord, ProviderConfig, RunRecord, RunState
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
    interruption = storage.get_events("run-active")[-1]
    assert interruption.type is EventType.STATE_CHANGED
    assert interruption.payload == {
        "state": "interrupted",
        "previous": "executing",
        "cause": "process_restart",
    }


def test_projects_provider_config_and_preferences(
    storage: Storage, settings: Settings, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = ProjectRecord(id="project-1", name="Project One", root=str(project_root.resolve()))
    storage.create_project(project)
    before_touch = storage.get_project(project.id).last_opened_at

    storage.create_run(
        RunRecord(
            id="project-run",
            task="Build",
            workspace=project.root,
            project_id=project.id,
        )
    )
    storage.touch_project(project.id)
    storage.set_preference("last_workspace", project.root)
    provider = ProviderConfig(
        model="deepseek-tool-model",
        base_url="https://provider.example/v1",
        credential_file=str(tmp_path / "credential"),
    )
    storage.save_provider_config(provider)

    assert storage.list_projects()[0].id == project.id
    assert storage.get_project(project.id).last_opened_at >= before_touch
    assert storage.list_runs(project_id=project.id)[0].project_id == project.id
    assert storage.list_runs()[0].id == "project-run"
    assert storage.get_preference("last_workspace") == project.root
    assert storage.get_provider_config(
        ProviderConfig(model=settings.model, base_url=settings.base_url)
    ).model == "deepseek-tool-model"

    with pytest.raises(ValueError, match="already uses"):
        storage.create_project(
            ProjectRecord(id="project-2", name="Duplicate", root=project.root)
        )


def test_mark_all_active_runs_interrupted(storage: Storage, settings: Settings) -> None:
    other = settings.workspace.parent / "other"
    other.mkdir()
    for run_id, workspace in (("one", settings.workspace), ("two", other)):
        storage.create_run(
            RunRecord(
                id=run_id,
                task="Work",
                workspace=str(workspace),
                state=RunState.EXECUTING,
            )
        )

    assert storage.mark_all_active_runs_interrupted() == 2
    assert storage.get_run("one").state is RunState.INTERRUPTED
    assert storage.get_run("two").state is RunState.INTERRUPTED
    assert all(
        storage.get_events(run_id)[-1].payload["cause"] == "process_restart"
        for run_id in ("one", "two")
    )


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
        assert loaded.project_id is None
        assert loaded.plan_gate is None
    finally:
        migrated.close()
