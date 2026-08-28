from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import pytest
from pydantic import ValidationError

from traceforge.config import Settings
from traceforge.models import (
    ApprovalMode,
    ConversationTurn,
    EventType,
    ProjectRecord,
    ProofPack,
    ProviderConfig,
    ReasoningEffort,
    RunEvent,
    RunRecord,
    RunState,
)
from traceforge.proof import build_proof_pack, build_success_proof_pack
from traceforge.storage import SecureCheckpointError, SnapshotRecord, Storage


def test_run_and_events_round_trip(storage: Storage, settings: Settings) -> None:
    workspace = settings.workspace
    run = RunRecord(
        id="run-1",
        task="Fix it",
        workspace=str(workspace),
        approval_mode=ApprovalMode.FULL_ACCESS,
        reasoning_effort=ReasoningEffort.HIGH,
        turns=[
            ConversationTurn(
                index=1,
                request="Fix it",
                approval_mode=ApprovalMode.FULL_ACCESS,
                reasoning_effort=ReasoningEffort.HIGH,
            )
        ],
    )
    storage.create_run(run)
    run.state = RunState.PLANNING
    run.messages.append({"role": "user", "content": "Fix it"})
    storage.save_run(run)

    event = storage.append_event("run-1", EventType.STATE_CHANGED, {"state": "planning"})
    loaded = storage.get_run("run-1")

    assert loaded.state is RunState.PLANNING
    assert loaded.approval_mode is ApprovalMode.FULL_ACCESS
    assert loaded.reasoning_effort is ReasoningEffort.HIGH
    assert loaded.turns[0].approval_mode is ApprovalMode.FULL_ACCESS
    assert loaded.turns[0].reasoning_effort is ReasoningEffort.HIGH
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


def test_proof_pack_storage_is_write_once_and_validates_persisted_json(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "proof.db"
    repository = Storage(database)
    run = RunRecord(
        id="proof-run",
        task="Prove it",
        workspace=str(workspace),
        state=RunState.SUCCEEDED,
        turns=[
            ConversationTurn(
                index=1,
                request="Prove it",
                outcome="succeeded",
                summary="Proven",
            )
        ],
    )
    repository.create_run(run)
    first = build_proof_pack(run, repository)
    stored = repository.save_proof_pack_if_absent(run.id, 1, first)
    replacement_payload = first.model_dump(mode="python", exclude={"artifact_sha256"})
    replacement_payload["task"] = "replacement must not win"
    replacement = ProofPack.seal(**replacement_payload)

    assert repository.save_proof_pack_if_absent(run.id, 1, replacement) == stored
    assert repository.get_proof_pack(run.id) == stored
    assert repository.get_proof_pack(run.id, 1) == stored
    assert repository.get_proof_pack(run.id, 2) is None
    repository.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE proof_packs SET proof_json = '{}' WHERE run_id = ? AND turn_index = ?",
        (run.id, 1),
    )
    connection.commit()
    connection.close()

    reopened = Storage(database)
    try:
        with pytest.raises(ValidationError):
            reopened.get_proof_pack(run.id, 1)
    finally:
        reopened.close()


def test_proof_pack_storage_rejects_hash_and_storage_identity_corruption(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "proof-corruption.db"
    repository = Storage(database)
    run = RunRecord(
        id="proof-identity",
        task="Keep proof identity",
        workspace=str(workspace),
        state=RunState.SUCCEEDED,
        turns=[
            ConversationTurn(
                index=1,
                request="Keep proof identity",
                outcome="succeeded",
                summary="Identity preserved",
            )
        ],
    )
    repository.create_run(run)
    pack = build_proof_pack(run, repository)
    repository.save_proof_pack_if_absent(run.id, 1, pack)
    repository.close()

    tampered = pack.model_dump(mode="json")
    tampered["task"] = "tampered without resealing"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE proof_packs SET proof_json = ? WHERE run_id = ? AND turn_index = 1",
            (json.dumps(tampered), run.id),
        )
    reopened = Storage(database)
    try:
        with pytest.raises(ValidationError, match="artifact SHA-256"):
            reopened.get_proof_pack(run.id, 1)
    finally:
        reopened.close()

    wrong_identity_payload = pack.model_dump(
        mode="python", exclude={"artifact_sha256"}
    )
    wrong_identity_payload["run_id"] = "different-run"
    wrong_identity = ProofPack.seal(**wrong_identity_payload)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE proof_packs SET proof_json = ? WHERE run_id = ? AND turn_index = 1",
            (wrong_identity.model_dump_json(), run.id),
        )
    reopened = Storage(database)
    try:
        with pytest.raises(ValueError, match="run id does not match"):
            reopened.get_proof_pack(run.id, 1)
    finally:
        reopened.close()


def test_atomic_success_commit_rolls_back_run_events_and_proof_together(
    storage: Storage, settings: Settings
) -> None:
    run = RunRecord(
        id="atomic-success",
        task="Commit success atomically",
        workspace=str(settings.workspace),
        state=RunState.VERIFYING,
        turns=[
            ConversationTurn(
                index=1,
                request="Commit success atomically",
                outcome="in_progress",
            )
        ],
    )
    storage.create_run(run)
    storage.append_event(run.id, EventType.STATE_CHANGED, {"state": "verifying"})
    before_events = storage.get_events(run.id)
    run.state = RunState.SUCCEEDED
    run.turns[-1].outcome = "succeeded"
    run.turns[-1].summary = "Committed"

    def fail_proof(_events: list[RunEvent]) -> ProofPack:
        raise RuntimeError("proof construction failed")

    with pytest.raises(RuntimeError, match="proof construction failed"):
        storage.commit_success(
            run,
            previous_state=RunState.VERIFYING,
            turn_payload={"index": 1, "outcome": "succeeded", "summary": "Committed"},
            completion_payload={"state": "succeeded", "diff": ""},
            proof_factory=fail_proof,
        )

    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.VERIFYING
    assert persisted.turns[-1].outcome == "in_progress"
    assert storage.get_events(run.id) == before_events
    assert storage.get_proof_pack(run.id) is None

    terminal_events, pack = storage.commit_success(
        run,
        previous_state=RunState.VERIFYING,
        turn_payload={"index": 1, "outcome": "succeeded", "summary": "Committed"},
        completion_payload={"state": "succeeded", "diff": ""},
        proof_factory=lambda events: build_success_proof_pack(run, storage, events),
    )

    assert [event.type for event in terminal_events] == [
        EventType.STATE_CHANGED,
        EventType.TURN_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert storage.get_run(run.id).state is RunState.SUCCEEDED
    assert storage.get_proof_pack(run.id, 1) == pack


def test_atomic_success_rejects_a_different_preexisting_turn_proof(
    storage: Storage, settings: Settings
) -> None:
    run = RunRecord(
        id="atomic-proof-conflict",
        task="Reject a conflicting frozen proof",
        workspace=str(settings.workspace),
        state=RunState.VERIFYING,
        turns=[
            ConversationTurn(
                index=1,
                request="Reject a conflicting frozen proof",
                outcome="in_progress",
            )
        ],
    )
    storage.create_run(run)
    storage.append_event(run.id, EventType.STATE_CHANGED, {"state": "verifying"})
    before_events = storage.get_events(run.id)
    already_frozen_run = run.model_copy(deep=True)
    already_frozen_run.state = RunState.SUCCEEDED
    already_frozen_run.turns[-1].outcome = "succeeded"
    already_frozen_run.turns[-1].summary = "Different historical result"
    already_frozen = build_proof_pack(
        already_frozen_run,
        storage,
        events=before_events,
        turn_index=1,
        event_through_seq=before_events[-1].seq,
    )
    storage.save_proof_pack_if_absent(run.id, 1, already_frozen)

    run.state = RunState.SUCCEEDED
    run.turns[-1].outcome = "succeeded"
    run.turns[-1].summary = "New result must not replace frozen evidence"
    with pytest.raises(ValueError, match="different Proof Pack already exists"):
        storage.commit_success(
            run,
            previous_state=RunState.VERIFYING,
            turn_payload={
                "index": 1,
                "outcome": "succeeded",
                "summary": run.turns[-1].summary,
            },
            completion_payload={"state": "succeeded", "diff": ""},
            proof_factory=lambda events: build_success_proof_pack(
                run, storage, events
            ),
        )

    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.VERIFYING
    assert persisted.turns[-1].outcome == "in_progress"
    assert storage.get_events(run.id) == before_events
    assert storage.get_proof_pack(run.id, 1) == already_frozen


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
            id="run-answered",
            task="Hello",
            workspace=str(workspace),
            state=RunState.ANSWERED,
        )
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
    assert storage.get_run("run-answered").state is RunState.ANSWERED
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
        context_window=240_000,
    )
    storage.save_provider_config(provider)

    assert storage.list_projects()[0].id == project.id
    assert storage.get_project(project.id).last_opened_at >= before_touch
    assert storage.list_runs(project_id=project.id)[0].project_id == project.id
    assert storage.list_runs()[0].id == "project-run"
    assert storage.get_preference("last_workspace") == project.root
    saved_provider = storage.get_provider_config(
        ProviderConfig(model=settings.model, base_url=settings.base_url)
    )
    assert saved_provider.model == "deepseek-tool-model"
    assert saved_provider.context_window == 240_000
    assert storage.get_run("project-run").context_limit == 64_000

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


def test_live_run_check_excludes_safely_interrupted_work(
    storage: Storage, settings: Settings
) -> None:
    paused = RunRecord(
        id="paused",
        task="Paused",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.EXECUTING,
    )
    storage.create_run(paused)
    assert storage.has_any_active_run() is True
    assert storage.has_live_run() is False

    active = RunRecord(
        id="active",
        task="Active",
        workspace=str(settings.workspace.parent),
        state=RunState.PLANNING,
    )
    storage.create_run(active)
    assert storage.has_live_run() is True


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
    connection.execute(
        """
        INSERT INTO runs(
            id, task, workspace, state, verifier_enabled, messages_json,
            created_at, updated_at
        ) VALUES (
            'legacy-success', 'Legacy success', ?, 'succeeded', 1, '[]',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        )
        """,
        (str(tmp_path),),
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
        assert loaded.approval_mode is ApprovalMode.AUTOMATIC
        assert loaded.reasoning_effort is ReasoningEffort.AUTO
        assert loaded.provider_reasoning_cleanup_pending is False
        assert migrated.get_proof_pack("new") is None
        assert migrated.is_proof_backfill_candidate("legacy-success") is True
        assert migrated.is_proof_backfill_candidate("new") is False
    finally:
        migrated.close()


def test_storage_migrates_and_clears_provider_verification(tmp_path: Path) -> None:
    database = tmp_path / "legacy-provider.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE provider_config (
            id INTEGER PRIMARY KEY CHECK(id = 1), model TEXT NOT NULL,
            base_url TEXT, credential_file TEXT, context_window INTEGER,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO provider_config(
            id, model, base_url, credential_file, context_window, updated_at
        ) VALUES (1, 'legacy-model', NULL, NULL, NULL, '2026-01-01T00:00:00+00:00')
        """
    )
    connection.commit()
    connection.close()

    migrated = Storage(database)
    try:
        assert migrated.get_provider_verified_at() is None
        config = migrated.get_provider_config(ProviderConfig(model="fallback"))
        verified_at = datetime(2026, 8, 28, tzinfo=UTC)

        migrated.save_provider_config(config, verified_at=verified_at)
        assert migrated.get_provider_verified_at() == verified_at

        migrated.save_provider_config(config)
        assert migrated.get_provider_verified_at() is None
    finally:
        migrated.close()


def test_storage_loads_a_real_legacy_row_with_auto_reasoning(tmp_path: Path) -> None:
    database = tmp_path / "legacy-row.db"
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
    connection.execute(
        """
        INSERT INTO runs(
            id, task, workspace, state, verifier_enabled, messages_json,
            created_at, updated_at
        ) VALUES (
            'legacy', 'Legacy task', ?, 'interrupted', 1, '[]',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        )
        """,
        (str(tmp_path),),
    )
    connection.commit()
    connection.close()

    migrated = Storage(database)
    try:
        loaded = migrated.get_run("legacy")
        assert loaded.reasoning_effort is ReasoningEffort.AUTO
        assert loaded.turns == []
        assert loaded.provider_reasoning_cleanup_pending is False
    finally:
        migrated.close()


def test_storage_files_are_owner_only(storage: Storage, settings: Settings) -> None:
    storage.create_run(
        RunRecord(id="private-db", task="Private", workspace=str(settings.workspace))
    )

    assert settings.data_dir.stat().st_mode & 0o077 == 0
    for database_file in settings.data_dir.glob("test.db*"):
        assert database_file.stat().st_mode & 0o077 == 0


def test_startup_scrubs_private_reasoning_from_legacy_terminal_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-private.db"
    sentinel = "PRIVATE-LEGACY-TERMINAL-SENTINEL"
    legacy = Storage(database)
    legacy.create_run(
        RunRecord(
            id="legacy-terminal",
            task="Already done",
            workspace=str(tmp_path),
            state=RunState.SUCCEEDED,
            messages=[
                {
                    "role": "assistant",
                    "content": "done",
                    "reasoning_content": sentinel,
                }
            ],
        )
    )
    legacy.secure_checkpoint()
    assert any(
        sentinel.encode() in database_file.read_bytes()
        for database_file in tmp_path.glob("legacy-private.db*")
    )
    legacy.close()

    reopened = Storage(database)
    try:
        loaded = reopened.get_run("legacy-terminal")
        assert loaded.state is RunState.SUCCEEDED
        assert loaded.provider_reasoning_cleanup_pending is False
        assert all("reasoning_content" not in message for message in loaded.messages)
        assert all(
            sentinel.encode() not in database_file.read_bytes()
            for database_file in tmp_path.glob("legacy-private.db*")
        )
    finally:
        reopened.close()


def test_secure_checkpoint_rejects_busy_wal_then_succeeds_after_reader_closes(
    storage: Storage, settings: Settings
) -> None:
    database = settings.data_dir / "test.db"
    sentinel = "PRIVATE-BUSY-WAL-SENTINEL"
    run = RunRecord(
        id="busy-wal",
        task="Scrub safely",
        workspace=str(settings.workspace),
        state=RunState.EXECUTING,
        messages=[
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": sentinel,
            }
        ],
    )
    storage.create_run(run)

    reader = sqlite3.connect(database)
    try:
        reader.execute("BEGIN")
        assert sentinel in reader.execute(
            "SELECT messages_json FROM runs WHERE id = 'busy-wal'"
        ).fetchone()[0]
        run.messages[0].pop("reasoning_content")
        storage.save_run(run)

        started = monotonic()
        with pytest.raises(SecureCheckpointError, match="WAL remained busy"):
            storage.secure_checkpoint()
        assert monotonic() - started < 2
        assert sentinel.encode() in Path(f"{database}-wal").read_bytes()
    finally:
        reader.rollback()
        reader.close()

    storage.secure_checkpoint(attempts=1, retry_delay=0)
    assert all(
        sentinel.encode() not in database_file.read_bytes()
        for database_file in settings.data_dir.glob("test.db*")
    )
