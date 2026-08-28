from __future__ import annotations

from traceforge.models import (
    AcceptanceCheck,
    CheckStatus,
    ConversationTurn,
    EventType,
    PlanGate,
    PlanStep,
    RunRecord,
    RunState,
    TaskPlan,
    ToolCall,
    ToolResult,
    Verdict,
    VerificationReport,
)
from traceforge.proof import build_proof_pack, proof_pack_markdown
from traceforge.storage import Storage
from traceforge.workspace import Workspace


def test_proof_pack_aggregates_stable_completion_evidence(
    storage: Storage, workspace: Workspace
) -> None:
    run = RunRecord(
        id="proof-run",
        task="Create proof.txt",
        workspace=str(workspace.root),
        state=RunState.SUCCEEDED,
        plan=TaskPlan(
            summary="Create one proof file",
            steps=[PlanStep(id="create", title="Create proof.txt", status="completed")],
            acceptance_checks=[
                AcceptanceCheck(
                    id="test",
                    label="Focused test",
                    command=["pytest", "-q"],
                    status=CheckStatus.PASSED,
                    exit_code=0,
                    evidence="1 passed",
                )
            ],
            impacted_files=["proof.txt"],
        ),
        plan_gate=PlanGate(
            decision="auto_approved",
            risk="low",
            reasons=["Explicit single-file scope"],
        ),
        verification=VerificationReport(
            verdict=Verdict.PASS,
            summary="The diff and focused test prove the requested file.",
        ),
        plan_approved=True,
        step_count=2,
    )
    storage.create_run(run)
    path = workspace.root / "proof.txt"
    workspace.snapshot(run.id, path)
    path.write_text("proven\n")
    workspace.record_agent_version(run.id, path)
    storage.append_event(run.id, EventType.PLAN_GATED, run.plan_gate.model_dump(mode="json"))
    command = ToolCall(
        id="check-command",
        name="run_command",
        arguments={"argv": ["pytest", "-q"]},
    )
    result = ToolResult(
        tool_call_id=command.id,
        name=command.name,
        ok=True,
        output="1 passed",
        metadata={
            "exit_code": 0,
            "sandbox": {
                "status": "enforced",
                "backend": "seatbelt",
                "enforced": True,
            },
        },
    )
    storage.append_event(
        run.id,
        EventType.TOOL_COMPLETED,
        {
            "call": command.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        },
    )
    completion_diff = workspace.diff(run.id)
    storage.append_event(
        run.id,
        EventType.RUN_COMPLETED,
        {"state": "succeeded", "diff": completion_diff},
    )

    first = build_proof_pack(storage.get_run(run.id), storage)
    path.write_text("later user edit\n")
    second = build_proof_pack(storage.get_run(run.id), storage)
    markdown = proof_pack_markdown(first)

    assert first.proof_status == "proven"
    assert first.checks_fresh is True
    assert first.changed_files == ["proof.txt"]
    assert "+proven" in first.diff
    assert "later user edit" not in second.diff
    assert first.diff_source == "completion_event"
    assert first.evidence_sha256 == second.evidence_sha256
    assert first.event_chain_sha256 == second.event_chain_sha256
    assert first.rollback.status == "available"
    assert first.command_sandbox.status == "enforced"
    assert first.command_sandbox.backends == ["seatbelt"]
    assert first.command_sandbox.sandboxed_commands == 1
    assert "TraceForge Proof Pack" in markdown
    assert first.evidence_sha256 in markdown
    assert "Independent read-only completion review" in markdown
    assert "## Command sandbox" in markdown


def test_proof_pack_marks_mixed_sandbox_execution(storage: Storage, workspace: Workspace) -> None:
    run = RunRecord(
        id="mixed-sandbox-proof",
        task="Run two commands",
        workspace=str(workspace.root),
        state=RunState.SUCCEEDED,
    )
    storage.create_run(run)
    for command_id, status in (("safe", "enforced"), ("approved", "bypassed")):
        storage.append_event(
            run.id,
            EventType.TOOL_COMPLETED,
            {
                "call": {
                    "id": command_id,
                    "name": "run_command",
                    "arguments": {"argv": ["python3", "-V"]},
                },
                "result": {
                    "tool_call_id": command_id,
                    "name": "run_command",
                    "ok": True,
                    "output": "Python",
                    "metadata": {
                        "sandbox": {
                            "status": status,
                            "backend": "seatbelt",
                            "enforced": status == "enforced",
                        }
                    },
                },
            },
        )
    storage.append_event(
        run.id,
        EventType.TOOL_COMPLETED,
        {
            "call": {
                "id": "rejected",
                "name": "run_command",
                "arguments": {"argv": ["curl", "https://example.invalid"]},
            },
            "result": {
                "tool_call_id": "rejected",
                "name": "run_command",
                "ok": False,
                "error": "User rejected this action.",
                "metadata": {},
            },
        },
    )

    pack = build_proof_pack(storage.get_run(run.id), storage)

    assert pack.command_sandbox.status == "mixed"
    assert pack.command_sandbox.sandboxed_commands == 1
    assert pack.command_sandbox.bypassed_commands == 1
    assert pack.command_sandbox.policy_only_commands == 0
    assert pack.command_sandbox.not_executed_commands == 1
    assert "Rejected or denied before execution: 1" in proof_pack_markdown(pack)


def test_proof_v1_digest_excludes_per_turn_navigation_hints(
    storage: Storage, workspace: Workspace
) -> None:
    run = RunRecord(
        id="turn-hint-proof",
        task="Create a file",
        workspace=str(workspace.root),
        state=RunState.SUCCEEDED,
        turns=[
            ConversationTurn(
                index=1,
                request="Create a file",
                outcome="succeeded",
                summary="Created it",
                changed_files=["created.txt"],
            )
        ],
    )
    storage.create_run(run)
    without_hint = run.model_copy(deep=True)
    without_hint.turns[0].changed_files = []

    with_hint_pack = build_proof_pack(run, storage)
    without_hint_pack = build_proof_pack(without_hint, storage)

    assert with_hint_pack.evidence_sha256 == without_hint_pack.evidence_sha256


def test_proof_pack_records_conflict_aware_rollback(
    storage: Storage, workspace: Workspace
) -> None:
    run = RunRecord(
        id="rollback-proof",
        task="Create generated.txt",
        workspace=str(workspace.root),
        state=RunState.ROLLED_BACK,
    )
    storage.create_run(run)
    path = workspace.root / "generated.txt"
    workspace.snapshot(run.id, path)
    path.write_text("temporary\n")
    workspace.record_agent_version(run.id, path)
    result = workspace.rollback(run.id)
    storage.append_event(
        run.id,
        EventType.ROLLBACK_COMPLETED,
        {
            "restored": result.restored,
            "removed": result.removed,
            "conflicts": result.conflicts,
        },
    )

    pack = build_proof_pack(storage.get_run(run.id), storage)

    assert pack.proof_status == "not_proven"
    assert pack.rollback.status == "completed"
    assert pack.rollback.removed == ["generated.txt"]
