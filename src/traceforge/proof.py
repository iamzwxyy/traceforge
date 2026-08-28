from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from traceforge.models import (
    CheckStatus,
    EventType,
    ProofCommandSandbox,
    ProofPack,
    ProofRollback,
    RunEvent,
    RunRecord,
    RunState,
    Verdict,
    utc_now,
)
from traceforge.storage import Storage
from traceforge.workspace import Workspace


def build_proof_pack(run: RunRecord, storage: Storage) -> ProofPack:
    events = storage.get_events(run.id)
    snapshots = storage.list_snapshots(run.id)
    diff, diff_source = _evidence_diff(run, storage, events)
    rollback = _rollback_evidence(events, has_snapshots=bool(snapshots))
    command_sandbox = _command_sandbox_evidence(events)
    proof_status = _proof_status(run)
    checks = run.plan.acceptance_checks if run.plan else []
    checks_fresh = bool(checks) and all(
        check.status is CheckStatus.PASSED for check in checks
    )
    event_chain_sha256 = _digest_json(
        [event.model_dump(mode="json") for event in events]
    )
    evidence: dict[str, Any] = {
        "schema_version": "traceforge.proof-pack.v1",
        "run_id": run.id,
        "task": run.task,
        "workspace": run.workspace,
        "project_id": run.project_id,
        "mode": run.mode.value,
        # v1 predates per-turn UI navigation, permission, and reasoning-setting hints. Keep its
        # stable digest surface unchanged; those choices remain covered by the event chain.
        "turns": [
            turn.model_dump(
                mode="json",
                exclude={"changed_files", "approval_mode", "reasoning_effort"},
            )
            for turn in run.turns
        ],
        "state": run.state.value,
        "proof_status": proof_status,
        "plan": run.plan.model_dump(mode="json") if run.plan else None,
        "plan_gate": run.plan_gate.model_dump(mode="json") if run.plan_gate else None,
        "changed_files": [snapshot.path for snapshot in snapshots],
        "diff": diff,
        "diff_source": diff_source,
        "diff_sha256": _digest_text(diff),
        "checks_fresh": checks_fresh,
        "verification": (
            run.verification.model_dump(mode="json") if run.verification else None
        ),
        "rollback": rollback.model_dump(mode="json"),
        "command_sandbox": command_sandbox.model_dump(mode="json"),
        "event_count": len(events),
        "event_chain_sha256": event_chain_sha256,
        "step_count": run.step_count,
        "repair_cycles": run.repair_cycles,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }
    presentation = {**evidence, "turns": run.turns}
    return ProofPack(
        generated_at=utc_now(),
        **presentation,
        evidence_sha256=_digest_json(evidence),
    )


def proof_pack_markdown(pack: ProofPack) -> str:
    lines = [
        f"# TraceForge Proof Pack · {pack.run_id[:8]}",
        "",
        f"- Proof status: **{pack.proof_status}**",
        f"- Run state: `{pack.state.value}`",
        (
            "- Current action-permission profile: "
            f"`{pack.turns[-1].approval_mode.value if pack.turns else 'automatic'}`"
        ),
        (
            "- Current requested reasoning effort: "
            f"`{pack.turns[-1].reasoning_effort.value if pack.turns else 'auto'}`"
        ),
        f"- Evidence SHA-256: `{pack.evidence_sha256}`",
        f"- Event chain SHA-256: `{pack.event_chain_sha256}` ({pack.event_count} events)",
        f"- Diff source: `{pack.diff_source}`",
        f"- Generated: {pack.generated_at.isoformat()}",
        "",
        "## Conversation",
        "",
        f"Workspace: `{pack.workspace}`",
    ]
    if pack.project_id:
        lines.append(f"Project ID: `{pack.project_id}`")
    lines.append("")
    if pack.turns:
        for turn in pack.turns:
            lines.extend(
                [
                    (
                        f"### Turn {turn.index} · {turn.mode.value} · "
                        f"{turn.approval_mode.value} approvals · "
                        f"{turn.reasoning_effort.value} reasoning"
                    ),
                    "",
                    turn.request,
                    "",
                    f"Outcome: **{turn.outcome}**",
                    turn.summary or "No summary recorded.",
                    "",
                ]
            )
    else:
        lines.extend([pack.task, ""])
    lines.extend(["## Plan and gate", ""])
    if pack.plan:
        lines.extend([pack.plan.markdown, ""])
        if pack.plan_gate:
            lines.append(
                f"Gate: **{pack.plan_gate.decision}** · risk **{pack.plan_gate.risk}**"
            )
            lines.extend(f"- {reason}" for reason in pack.plan_gate.reasons)
            lines.append("")
    else:
        lines.append("No plan has been recorded yet.")
    lines.extend(["", "## Changed files", ""])
    lines.extend(f"- `{path}`" for path in pack.changed_files)
    if not pack.changed_files:
        lines.append("No agent-authored file snapshots.")
    lines.extend(["", "## Unified diff", "", "```diff", pack.diff or "(no diff)", "```"])
    lines.extend(["", "## Acceptance evidence", ""])
    if pack.plan:
        lines.extend(
            [
                "| Check | Status | Command | Evidence |",
                "| --- | --- | --- | --- |",
                *(
                    "| "
                    + " | ".join(
                        [
                            _table(check.label),
                            check.status.value,
                            _table(" ".join(check.command or [])),
                            _table(check.evidence),
                        ]
                    )
                    + " |"
                    for check in pack.plan.acceptance_checks
                ),
            ]
        )
    else:
        lines.append("No acceptance evidence yet.")
    lines.extend(["", "## Independent read-only completion review", ""])
    if pack.verification:
        lines.append(f"Verdict: **{pack.verification.verdict.value}**")
        lines.append("")
        lines.append(pack.verification.summary)
        for finding in pack.verification.findings:
            lines.append(f"- **{finding.severity}: {finding.title}** — {finding.evidence}")
    else:
        lines.append("No independent verdict has been recorded yet.")
    lines.extend(
        [
            "",
            "## Conflict-aware rollback",
            "",
            f"Status: **{pack.rollback.status}**",
            f"- Restored: {', '.join(pack.rollback.restored) or 'none'}",
            f"- Removed: {', '.join(pack.rollback.removed) or 'none'}",
            f"- Conflicts preserved: {', '.join(pack.rollback.conflicts) or 'none'}",
            "",
            "## Command sandbox",
            "",
            f"Status: **{pack.command_sandbox.status}**",
            f"- Backends: {', '.join(pack.command_sandbox.backends) or 'none'}",
            f"- OS-sandboxed commands: {pack.command_sandbox.sandboxed_commands}",
            f"- User-approved bypasses: {pack.command_sandbox.bypassed_commands}",
            f"- Policy-only commands: {pack.command_sandbox.policy_only_commands}",
            f"- Rejected or denied before execution: {pack.command_sandbox.not_executed_commands}",
            "",
        ]
    )
    return "\n".join(lines)


def _proof_status(
    run: RunRecord,
) -> Literal["in_progress", "proven", "checks_only", "not_proven"]:
    if not run.state.terminal and run.state is not RunState.INTERRUPTED:
        return "in_progress"
    if run.state is RunState.SUCCEEDED:
        if run.verification and run.verification.verdict is Verdict.PASS:
            return "proven"
        return "checks_only"
    return "not_proven"


def _rollback_evidence(
    events: list[RunEvent], *, has_snapshots: bool
) -> ProofRollback:
    completed = [event for event in events if event.type is EventType.ROLLBACK_COMPLETED]
    if completed:
        payload = completed[-1].payload
        return ProofRollback(
            status="completed",
            restored=list(payload.get("restored", [])),
            removed=list(payload.get("removed", [])),
            conflicts=list(payload.get("conflicts", [])),
        )
    return ProofRollback(status="available" if has_snapshots else "not_available")


def _command_sandbox_evidence(events: list[RunEvent]) -> ProofCommandSandbox:
    statuses: list[str] = []
    backends: set[str] = set()
    not_executed = 0
    for event in events:
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        call = event.payload.get("call")
        result = event.payload.get("result")
        if not isinstance(call, dict) or call.get("name") != "run_command":
            continue
        if not isinstance(result, dict):
            continue
        metadata = result.get("metadata")
        sandbox = metadata.get("sandbox") if isinstance(metadata, dict) else None
        if not isinstance(sandbox, dict):
            if isinstance(metadata, dict) and (
                "exit_code" in metadata or metadata.get("timeout") is True
            ):
                statuses.append("policy_only")
            else:
                not_executed += 1
            continue
        status = str(sandbox.get("status", "policy_only"))
        if status not in {"enforced", "bypassed", "policy_only"}:
            status = "policy_only"
        statuses.append(status)
        backend = str(sandbox.get("backend", "none"))
        if backend != "none":
            backends.add(backend)
    counts = {
        status: statuses.count(status)
        for status in ("enforced", "bypassed", "policy_only")
    }
    overall: Literal["enforced", "mixed", "bypassed", "policy_only", "not_used"]
    if not statuses:
        overall = "not_used"
    elif len(set(statuses)) > 1:
        overall = "mixed"
    else:
        single = statuses[0]
        if single == "enforced":
            overall = "enforced"
        elif single == "bypassed":
            overall = "bypassed"
        else:
            overall = "policy_only"
    return ProofCommandSandbox(
        status=overall,
        backends=sorted(backends),
        sandboxed_commands=counts["enforced"],
        bypassed_commands=counts["bypassed"],
        policy_only_commands=counts["policy_only"],
        not_executed_commands=not_executed,
    )


def _evidence_diff(
    run: RunRecord,
    storage: Storage,
    events: list[RunEvent],
) -> tuple[str, Literal["completion_event", "diff_event", "live_workspace"]]:
    for event in reversed(events):
        if event.type is EventType.RUN_COMPLETED:
            diff = event.payload.get("diff")
            if isinstance(diff, str):
                return diff, "completion_event"
    for event in reversed(events):
        if event.type is EventType.DIFF_UPDATED:
            diff = event.payload.get("diff")
            if isinstance(diff, str):
                return diff, "diff_event"
    return Workspace(Path(run.workspace), storage).diff(run.id), "live_workspace"


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_json(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest_text(rendered)


def _table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")
