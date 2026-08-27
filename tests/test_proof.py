from __future__ import annotations

from traceforge.models import (
    AcceptanceCheck,
    CheckStatus,
    EventType,
    PlanGate,
    PlanStep,
    RunRecord,
    RunState,
    TaskPlan,
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
    assert "TraceForge Proof Pack" in markdown
    assert first.evidence_sha256 in markdown
    assert "Independent verification" in markdown


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
