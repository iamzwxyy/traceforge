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
    ApprovalRequest,
    ConversationTurn,
    DecisionKind,
    DecisionStatus,
    EventType,
    ProjectRecord,
    ProofPack,
    ProviderConfig,
    ReasoningEffort,
    RunEvent,
    RunRecord,
    RunState,
    ToolCall,
    ToolResult,
)
from traceforge.proof import build_proof_pack, build_success_proof_pack
from traceforge.storage import (
    CredentialPersistenceError,
    SecureCheckpointError,
    SnapshotRecord,
    Storage,
)


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


def test_run_lineage_requires_a_rolled_back_parent_and_is_atomic(
    storage: Storage, settings: Settings
) -> None:
    parent = RunRecord(
        id="parent",
        task="First change",
        workspace=str(settings.workspace),
        state=RunState.ROLLED_BACK,
    )
    storage.create_run(parent)
    child = RunRecord(
        id="child",
        task="Continue safely",
        workspace=str(settings.workspace),
    )

    storage.create_run(child, parent_run_id=parent.id)

    assert storage.get_parent_run_id(child.id) == parent.id
    assert storage.get_parent_run_id(parent.id) is None
    assert storage.get_successor_run_id(parent.id) == child.id
    assert storage.get_successor_run_id(child.id) is None

    invalid_parent = RunRecord(
        id="active-parent",
        task="Still active",
        workspace=str(settings.workspace),
        state=RunState.EXECUTING,
    )
    storage.create_run(invalid_parent)
    rejected_child = RunRecord(
        id="rejected-child",
        task="Must not persist",
        workspace=str(settings.workspace),
    )
    with pytest.raises(ValueError, match="rolled-back"):
        storage.create_run(rejected_child, parent_run_id=invalid_parent.id)
    with pytest.raises(KeyError):
        storage.get_run(rejected_child.id)


def test_open_decision_rolls_back_waiting_state_row_and_events_together(
    storage: Storage,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = RunRecord(
        id="atomic-decision",
        task="Choose safely",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
    )
    storage.create_run(run)
    run.state = RunState.AWAITING_PLAN_APPROVAL
    inserted = 0
    original_insert = storage._insert_event_locked

    def fail_second_event(event: RunEvent) -> None:
        nonlocal inserted
        inserted += 1
        original_insert(event)
        if inserted == 2:
            raise RuntimeError("event write failed")

    monkeypatch.setattr(storage, "_insert_event_locked", fail_second_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        storage.open_decision(
            run,
            previous_state=RunState.PLANNING,
            request_id="decision-1",
            kind=DecisionKind.PLAN,
            turn_index=1,
            subject={"summary": "Review"},
            requested_event_type=EventType.PLAN_UPDATED,
            requested_payload={"summary": "Review"},
        )

    assert storage.get_run(run.id).state is RunState.PLANNING
    assert storage.get_active_decision(run.id) is None
    assert storage.get_events(run.id) == []


def test_approved_action_consumption_and_start_marker_are_atomic(
    storage: Storage,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(
        id="command",
        name="run_command",
        arguments={"argv": ["git", "status"]},
    )
    approval = ApprovalRequest(
        id="approval-1",
        tool_call=call,
        summary="Inspect repository status",
        reason="Manual approval is required",
        risk="elevated",
        approval_mode=ApprovalMode.MANUAL,
        policy_decision="ask",
    )
    run = RunRecord(
        id="atomic-action",
        task="Inspect status",
        workspace=str(settings.workspace),
        state=RunState.EXECUTING,
        pending_approval=approval,
    )
    storage.create_run(run)
    run.state = RunState.AWAITING_ACTION_APPROVAL
    storage.open_decision(
        run,
        previous_state=RunState.EXECUTING,
        request_id=approval.id,
        kind=DecisionKind.ACTION,
        turn_index=1,
        subject=approval.model_dump(mode="json"),
        requested_event_type=EventType.APPROVAL_REQUESTED,
        requested_payload=approval.model_dump(mode="json"),
    )
    storage.accept_decision(
        run.id,
        approval.id,
        DecisionKind.ACTION,
        {"approved": True},
    )
    events_before = storage.get_events(run.id)
    persisted = storage.get_run(run.id)
    persisted.pending_approval = None
    persisted.state = RunState.EXECUTING
    original_insert = storage._insert_event_locked

    def fail_start_marker(event: RunEvent) -> None:
        original_insert(event)
        if event.type is EventType.TOOL_STARTED:
            raise RuntimeError("start marker write failed")

    monkeypatch.setattr(storage, "_insert_event_locked", fail_start_marker)
    with pytest.raises(RuntimeError, match="start marker write failed"):
        storage.consume_decision(
            persisted,
            approval.id,
            DecisionKind.ACTION,
            previous_state=RunState.AWAITING_ACTION_APPROVAL,
            resolved_event_type=EventType.APPROVAL_RESOLVED,
            resolved_payload={"approved": True},
            action_call_payload=call.model_dump(mode="json"),
        )

    unchanged = storage.get_run(run.id)
    assert unchanged.state is RunState.AWAITING_ACTION_APPROVAL
    assert unchanged.pending_approval == approval
    receipt = storage.get_decision(run.id, approval.id)
    assert receipt.status is DecisionStatus.ACCEPTED
    assert receipt.execution_started_at is None
    assert storage.get_events(run.id) == events_before


def test_rejected_action_result_and_decision_consumption_are_atomic(
    storage: Storage,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(
        id="rejected-command",
        name="run_command",
        arguments={"argv": ["git", "status"]},
    )
    approval = ApprovalRequest(
        id="approval-rejected",
        tool_call=call,
        summary="Inspect repository status",
        reason="Manual approval is required",
        risk="elevated",
        approval_mode=ApprovalMode.MANUAL,
        policy_decision="ask",
    )
    run = RunRecord(
        id="atomic-rejection",
        task="Reject status inspection",
        workspace=str(settings.workspace),
        state=RunState.EXECUTING,
        pending_approval=approval,
    )
    storage.create_run(run)
    run.state = RunState.AWAITING_ACTION_APPROVAL
    storage.open_decision(
        run,
        previous_state=RunState.EXECUTING,
        request_id=approval.id,
        kind=DecisionKind.ACTION,
        turn_index=1,
        subject=approval.model_dump(mode="json"),
        requested_event_type=EventType.APPROVAL_REQUESTED,
        requested_payload=approval.model_dump(mode="json"),
    )
    storage.accept_decision(
        run.id,
        approval.id,
        DecisionKind.ACTION,
        {"approved": False},
    )
    events_before = storage.get_events(run.id)
    rejected = ToolResult(
        tool_call_id=call.id,
        name=call.name,
        ok=False,
        error="User rejected this action.",
    )
    persisted = storage.get_run(run.id)
    persisted.pending_approval = None
    persisted.state = RunState.EXECUTING
    persisted.messages.append(
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": rejected.model_dump_json(),
        }
    )
    original_insert = storage._insert_event_locked

    def fail_completed_marker(event: RunEvent) -> None:
        original_insert(event)
        if event.type is EventType.TOOL_COMPLETED:
            raise RuntimeError("completion marker write failed")

    monkeypatch.setattr(storage, "_insert_event_locked", fail_completed_marker)
    with pytest.raises(RuntimeError, match="completion marker write failed"):
        storage.consume_decision(
            persisted,
            approval.id,
            DecisionKind.ACTION,
            previous_state=RunState.AWAITING_ACTION_APPROVAL,
            resolved_event_type=EventType.APPROVAL_RESOLVED,
            resolved_payload={"approved": False},
            completed_tool_payload={
                "call": call.model_dump(mode="json"),
                "result": rejected.model_dump(mode="json"),
                "approval_request_id": approval.id,
            },
        )

    unchanged = storage.get_run(run.id)
    assert unchanged.state is RunState.AWAITING_ACTION_APPROVAL
    assert unchanged.pending_approval == approval
    assert unchanged.messages == []
    assert storage.get_decision(run.id, approval.id).status is DecisionStatus.ACCEPTED
    assert storage.get_events(run.id) == events_before


def test_abandon_decision_clears_subject_and_emits_evidence_atomically(
    storage: Storage,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(id="pending", name="run_command", arguments={"argv": ["pwd"]})
    approval = ApprovalRequest(
        id="approval-abandon",
        tool_call=call,
        summary="Inspect workspace",
        reason="Manual approval is required",
        risk="elevated",
        approval_mode=ApprovalMode.MANUAL,
        policy_decision="ask",
    )
    run = RunRecord(
        id="atomic-abandon",
        task="Abandon safely",
        workspace=str(settings.workspace),
        state=RunState.EXECUTING,
        pending_approval=approval,
    )
    storage.create_run(run)
    run.state = RunState.AWAITING_ACTION_APPROVAL
    storage.open_decision(
        run,
        previous_state=RunState.EXECUTING,
        request_id=approval.id,
        kind=DecisionKind.ACTION,
        turn_index=1,
        subject=approval.model_dump(mode="json"),
        requested_event_type=EventType.APPROVAL_REQUESTED,
        requested_payload=approval.model_dump(mode="json"),
    )
    storage.accept_decision(
        run.id,
        approval.id,
        DecisionKind.ACTION,
        {"approved": True},
    )
    events_before = storage.get_events(run.id)
    persisted = storage.get_run(run.id)
    persisted.pending_approval = None
    original_insert = storage._insert_event_locked

    def fail_abandon_event(event: RunEvent) -> None:
        original_insert(event)
        if event.type is EventType.APPROVAL_RESOLVED:
            raise RuntimeError("abandon evidence write failed")

    monkeypatch.setattr(storage, "_insert_event_locked", fail_abandon_event)
    with pytest.raises(RuntimeError, match="abandon evidence write failed"):
        storage.abandon_decision(
            persisted,
            approval.id,
            event_type=EventType.APPROVAL_RESOLVED,
            event_payload={"outcome": "abandoned"},
        )

    unchanged = storage.get_run(run.id)
    assert unchanged.pending_approval == approval
    assert storage.get_decision(run.id, approval.id).status is DecisionStatus.ACCEPTED
    assert storage.get_events(run.id) == events_before


def test_rollback_state_and_replayable_result_are_atomic(
    storage: Storage,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = RunRecord(
        id="atomic-rollback",
        task="Rollback atomically",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
    )
    storage.create_run(run)
    run.state = RunState.ROLLED_BACK
    original_insert = storage._insert_event_locked

    def fail_result_event(event: RunEvent) -> None:
        original_insert(event)
        if event.type is EventType.ROLLBACK_COMPLETED:
            raise RuntimeError("rollback result write failed")

    monkeypatch.setattr(storage, "_insert_event_locked", fail_result_event)
    with pytest.raises(RuntimeError, match="rollback result write failed"):
        storage.commit_rollback(
            run,
            previous_state=RunState.INTERRUPTED,
            rollback_payload={
                "restored": ["restored.txt"],
                "removed": [],
                "conflicts": [],
            },
        )

    assert storage.get_run(run.id).state is RunState.INTERRUPTED
    assert storage.get_events(run.id) == []


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


@pytest.mark.parametrize(
    ("terminal_state", "outcome"),
    [
        (RunState.ANSWERED, "answered"),
        (RunState.FAILED, "failed"),
        (RunState.CANCELLED, "cancelled"),
    ],
)
def test_answered_failed_and_cancelled_turns_commit_atomically(
    storage: Storage,
    settings: Settings,
    terminal_state: RunState,
    outcome: str,
) -> None:
    run = RunRecord(
        id=f"atomic-{outcome}",
        task=f"Commit {outcome} atomically",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
        turns=[
            ConversationTurn(
                index=1,
                request=f"Commit {outcome} atomically",
                outcome="in_progress",
            )
        ],
    )
    storage.create_run(run)
    storage.append_event(run.id, EventType.STATE_CHANGED, {"state": "planning"})
    run.state = terminal_state
    run.turns[-1].outcome = outcome  # type: ignore[assignment]
    run.turns[-1].summary = f"Terminal {outcome}"

    events = storage.commit_terminal_turn(
        run,
        previous_state=RunState.PLANNING,
        turn_payload={"index": 1, "outcome": outcome, "summary": f"Terminal {outcome}"},
        completion_payload={"state": terminal_state.value},
    )

    assert [event.type for event in events] == [
        EventType.STATE_CHANGED,
        EventType.TURN_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    persisted = storage.get_run(run.id)
    assert persisted.state is terminal_state
    assert persisted.turns[-1].outcome == outcome


def test_atomic_terminal_turn_rolls_back_state_turn_and_events_together(
    storage: Storage,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = RunRecord(
        id="atomic-answer-fault",
        task="Do not leave an answered ghost",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
        turns=[
            ConversationTurn(index=1, request="Do not leave an answered ghost")
        ],
    )
    storage.create_run(run)
    storage.append_event(run.id, EventType.STATE_CHANGED, {"state": "planning"})
    before_events = storage.get_events(run.id)
    run.state = RunState.ANSWERED
    run.turns[-1].outcome = "answered"
    run.turns[-1].summary = "This answer must be atomic"
    original_insert = storage._insert_event_locked

    def fail_turn_event(event: RunEvent) -> None:
        original_insert(event)
        if event.type is EventType.TURN_COMPLETED:
            raise RuntimeError("terminal turn write failed")

    monkeypatch.setattr(storage, "_insert_event_locked", fail_turn_event)
    with pytest.raises(RuntimeError, match="terminal turn write failed"):
        storage.commit_terminal_turn(
            run,
            previous_state=RunState.PLANNING,
            turn_payload={
                "index": 1,
                "outcome": "answered",
                "summary": "This answer must be atomic",
            },
            completion_payload={"state": RunState.ANSWERED.value},
        )

    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.PLANNING
    assert persisted.turns[-1].outcome == "in_progress"
    assert storage.get_events(run.id) == before_events


def test_interruption_closes_stream_updates_run_and_emits_events_atomically(
    storage: Storage,
    settings: Settings,
) -> None:
    run = RunRecord(
        id="atomic-interruption",
        task="Pause without leaving an open draft",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
        turns=[
            ConversationTurn(
                index=1,
                request="Pause without leaving an open draft",
                summary_stream_id="draft-stream",
            )
        ],
    )
    storage.create_run(run)
    storage.append_event(
        run.id,
        EventType.ASSISTANT_OUTPUT_STARTED,
        {"stream_id": "draft-stream", "status": "streaming"},
    )
    run.state = RunState.INTERRUPTED
    run.interrupted_from = RunState.PLANNING
    run.error = "Provider unavailable"
    run.turns[-1].summary_stream_id = None

    events = storage.commit_interruption(
        run,
        previous_state=RunState.PLANNING,
        stream_status="interrupted",
        stream_reason="model_unavailable",
        error_payload={"message": run.error, "recoverable": True},
        state_payload={
            "state": "interrupted",
            "previous": "planning",
            "cause": "model_unavailable",
        },
    )

    assert [event.type for event in events] == [
        EventType.ASSISTANT_OUTPUT_ABORTED,
        EventType.ERROR,
        EventType.STATE_CHANGED,
    ]
    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.INTERRUPTED
    assert persisted.interrupted_from is RunState.PLANNING
    assert persisted.turns[-1].summary_stream_id is None
    assert storage.list_open_assistant_output_streams(run.id) == []


def test_interruption_commit_fault_rolls_back_stream_state_and_events_together(
    storage: Storage,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = RunRecord(
        id="atomic-interruption-fault",
        task="Remain recoverable after an event write fault",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
        turns=[
            ConversationTurn(
                index=1,
                request="Remain recoverable after an event write fault",
                summary_stream_id="fault-stream",
            )
        ],
    )
    storage.create_run(run)
    storage.append_event(
        run.id,
        EventType.ASSISTANT_OUTPUT_STARTED,
        {"stream_id": "fault-stream", "status": "streaming"},
    )
    before_events = storage.get_events(run.id)
    run.state = RunState.INTERRUPTED
    run.interrupted_from = RunState.PLANNING
    run.turns[-1].summary_stream_id = None
    original_insert = storage._insert_event_locked

    def fail_state_event(event: RunEvent) -> None:
        original_insert(event)
        if event.type is EventType.STATE_CHANGED:
            raise RuntimeError("interruption state event write failed")

    monkeypatch.setattr(storage, "_insert_event_locked", fail_state_event)
    with pytest.raises(RuntimeError, match="interruption state event write failed"):
        storage.commit_interruption(
            run,
            previous_state=RunState.PLANNING,
            stream_status="interrupted",
            stream_reason="model_unavailable",
            state_payload={"state": "interrupted", "previous": "planning"},
        )

    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.PLANNING
    assert persisted.interrupted_from is None
    assert persisted.turns[-1].summary_stream_id == "fault-stream"
    assert storage.get_events(run.id) == before_events
    assert storage.list_open_assistant_output_streams(run.id) == [
        {"stream_id": "fault-stream", "status": "streaming"}
    ]


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


def test_registered_credential_guard_rejects_run_event_and_snapshot_writes(
    storage: Storage, settings: Settings
) -> None:
    configured = 'owner"secret\\tail\tsegment'
    storage.register_credential_guard(configured)

    with pytest.raises(CredentialPersistenceError):
        storage.create_run(
            RunRecord(
                id="unsafe-run",
                task=f"Inspect {configured}",
                workspace=str(settings.workspace),
            )
        )

    storage.create_run(
        RunRecord(id="safe-run", task="Inspect safely", workspace=str(settings.workspace))
    )
    with pytest.raises(CredentialPersistenceError):
        storage.append_event(
            "safe-run",
            EventType.MESSAGE,
            {"content": configured},
        )
    with pytest.raises(CredentialPersistenceError):
        storage.save_snapshot_if_absent(
            SnapshotRecord(
                run_id="safe-run",
                path="unsafe.txt",
                existed=True,
                content=configured.encode(),
                mode=0o600,
                original_hash=None,
                last_agent_hash=None,
            )
        )

    assert [run.id for run in storage.list_runs()] == ["safe-run"]
    assert storage.get_events("safe-run") == []
    assert storage.list_snapshots("safe-run") == []
    escaped = json.dumps(configured, ensure_ascii=False)[1:-1].encode()
    for database_file in settings.data_dir.glob("test.db*"):
        database_bytes = database_file.read_bytes()
        assert configured.encode() not in database_bytes
        assert escaped not in database_bytes


def test_boundary_safe_storage_and_public_json_prevent_structural_synthesis(
    storage: Storage, settings: Settings
) -> None:
    configured = 'foo", "start_line": 1'
    storage.register_credential_guard(configured)
    storage.create_run(
        RunRecord(id="structured", task="Inspect safely", workspace=str(settings.workspace))
    )

    event = storage.append_event(
        "structured",
        EventType.TOOL_REQUESTED,
        {"path": "foo", "start_line": 1},
    )
    rendered = storage.render_public_json(event.model_dump(mode="json"))
    storage.secure_checkpoint()

    assert json.loads(rendered)["payload"] == {"path": "foo", "start_line": 1}
    assert configured.encode() not in rendered
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


def test_upgrade_abandons_pre_boundary_active_action_decisions(tmp_path: Path) -> None:
    database = tmp_path / "legacy-action.db"
    repository = Storage(database)
    approval = ApprovalRequest(
        id="legacy-approval",
        tool_call=ToolCall(
            id="legacy-call",
            name="run_command",
            arguments={"argv": ["git", "status"]},
        ),
        summary="Inspect status",
        reason="Approval required",
        risk="elevated",
    )
    run = RunRecord(
        id="legacy-action",
        task="Inspect",
        workspace=str(tmp_path),
        state=RunState.EXECUTING,
        pending_approval=approval,
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "legacy-call",
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": '{"argv":["git","status"]}',
                        },
                    }
                ],
            }
        ],
    )
    repository.create_run(run)
    run.state = RunState.AWAITING_ACTION_APPROVAL
    repository.open_decision(
        run,
        previous_state=RunState.EXECUTING,
        request_id=approval.id,
        kind=DecisionKind.ACTION,
        turn_index=1,
        subject=approval.model_dump(mode="json"),
        requested_event_type=EventType.APPROVAL_REQUESTED,
        requested_payload=approval.model_dump(mode="json"),
    )
    with sqlite3.connect(database) as legacy_writer:
        legacy_writer.execute(
            "DELETE FROM schema_migrations WHERE name = 'credential-boundary-v1'"
        )
    repository.close()

    migrated = Storage(database)
    try:
        migrated_run = migrated.get_run(run.id)
        assert migrated_run.pending_approval is None
        assert migrated_run.messages == []
        assert migrated.get_active_decision(run.id) is None
        assert migrated.get_decision(run.id, approval.id).status is DecisionStatus.ABANDONED
        assert migrated.get_events(run.id)[-1].payload == {
            "kind": DecisionKind.ACTION.value,
            "cause": "credential_boundary_upgrade",
        }
    finally:
        migrated.close()


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
