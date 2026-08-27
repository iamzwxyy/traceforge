from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from traceforge.agent import AgentManager, InvalidRunAction, PlanDecision, RunConflictError
from traceforge.config import Settings
from traceforge.models import (
    ClarificationAnswer,
    EventType,
    PlanGate,
    RunRecord,
    RunState,
    TaskPlan,
    ToolCall,
    Verdict,
)
from traceforge.provider import ModelResponse, ScriptedProvider
from traceforge.storage import Storage


async def _wait_for_state(
    storage: Storage,
    run_id: str,
    state: RunState,
    *,
    deadline_seconds: float = 3,
) -> None:
    async with asyncio.timeout(deadline_seconds):
        while storage.get_run(run_id).state is not state:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def _plan_arguments(command: list[str]) -> dict[str, object]:
    return {
        "summary": "Create a verified result",
        "steps": [{"id": "implement", "title": "Create the requested file"}],
        "acceptance_checks": [
            {"id": "check", "label": "Result is correct", "command": command}
        ],
        "risks": ["The file must stay inside the workspace"],
    }


@pytest.mark.asyncio
async def test_full_clarify_build_verify_flow(
    settings: Settings, storage: Storage
) -> None:
    check = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('result.txt').read_text() == 'hello\\n'",
    ]
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="questions",
                        name="ask_questions",
                        arguments={
                            "questions": [
                                {
                                    "id": "format",
                                    "prompt": "Which output format?",
                                    "options": [
                                        {
                                            "id": "text",
                                            "label": "Text",
                                            "description": "Plain text",
                                            "recommended": True,
                                        },
                                        {
                                            "id": "json",
                                            "label": "JSON",
                                            "description": "Structured output",
                                        },
                                    ],
                                }
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_plan_arguments(check))
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create",
                        name="create_file",
                        arguments={"path": "result.txt", "content": "hello\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="check", name="run_command", arguments={"argv": check})
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Created result.txt", "evidence": ["check passed"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify",
                        name="submit_verification",
                        arguments={
                            "verdict": "pass",
                            "summary": "The diff and command evidence satisfy the task.",
                            "findings": [],
                        },
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create result.txt after clarifying the format")

    await _wait_for_state(storage, run.id, RunState.AWAITING_CLARIFICATION)
    await manager.answer_clarification(
        run.id, [ClarificationAnswer(question_id="format", option_id="text")]
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.verification is not None
    assert completed.verification.verdict is Verdict.PASS
    assert (settings.workspace / "result.txt").read_text() == "hello\n"
    event_types = [event.type for event in storage.get_events(run.id)]
    assert EventType.CLARIFICATION_REQUESTED in event_types
    assert EventType.DIFF_UPDATED in event_types
    assert EventType.VERIFICATION_COMPLETED in event_types


@pytest.mark.asyncio
async def test_low_risk_single_file_plan_is_auto_approved_but_stays_visible(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="submit_plan",
                        arguments={
                            "summary": "Create one note",
                            "steps": [{"id": "create", "title": "Create the note"}],
                            "acceptance_checks": [
                                {"id": "review", "label": "The note is correct"}
                            ],
                            "impacted_files": ["note.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create",
                        name="create_file",
                        arguments={"path": "note.txt", "content": "ready\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Created note", "evidence": ["diff"]},
                    )
                ]
            ),
            _verification("pass", "The single-file change satisfies the task."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create note.txt")

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.plan is not None
    assert completed.plan_gate is not None
    assert completed.plan_gate.decision == "auto_approved"
    state_events = [
        event.payload["state"]
        for event in storage.get_events(run.id)
        if event.type is EventType.STATE_CHANGED
    ]
    assert RunState.AWAITING_PLAN_APPROVAL.value not in state_events
    assert EventType.PLAN_GATED in [event.type for event in storage.get_events(run.id)]


@pytest.mark.asyncio
async def test_fast_path_scope_drift_requires_action_approval(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="submit_plan",
                        arguments={
                            "summary": "Create one allowed file",
                            "steps": [{"id": "create", "title": "Create the file"}],
                            "acceptance_checks": [
                                {"id": "review", "label": "The file is correct"}
                            ],
                            "impacted_files": ["allowed.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="drift",
                        name="create_file",
                        arguments={"path": "outside-plan.txt", "content": "drift\n"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create allowed.txt")

    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    waiting = storage.get_run(run.id)

    assert waiting.pending_approval is not None
    assert "exceeds the visible plan scope" in waiting.pending_approval.reason
    assert not (settings.workspace / "outside-plan.txt").exists()
    await manager.cancel(run.id)


@pytest.mark.asyncio
async def test_unknown_command_waits_for_explicit_approval(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="submit_plan",
                        arguments={
                            "summary": "Inspect Python",
                            "steps": [{"id": "inspect", "title": "Run Python"}],
                            "acceptance_checks": [
                                {"id": "observe", "label": "Output was inspected"}
                            ],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="python",
                        name="run_command",
                        arguments={"argv": ["python3", "-c", "print('hello')"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Observed", "evidence": ["command was rejected"]},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Inspect the Python environment", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)

    await manager.decide_action(run.id, approved=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    approvals = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.APPROVAL_RESOLVED
    ]
    assert approvals[0].payload["approved"] is False


def _review_plan(summary: str = "Review the workspace") -> dict[str, object]:
    return {
        "summary": summary,
        "steps": [{"id": "review", "title": "Review current behavior"}],
        "acceptance_checks": [{"id": "reviewed", "label": "Behavior was reviewed"}],
    }


def _verification(verdict: str, summary: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                id=f"verify-{verdict}",
                name="submit_verification",
                arguments={"verdict": verdict, "summary": summary, "findings": []},
            )
        ]
    )


@pytest.mark.asyncio
async def test_builder_reuses_successful_planning_inspection(
    settings: Settings, storage: Storage
) -> None:
    (settings.workspace / "context.txt").write_text("important evidence\n")
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="inspect",
                        name="read_file",
                        arguments={"path": "context.txt"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Reviewed", "evidence": ["planner read"]},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Review context.txt", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    builder_messages = provider.requests[2][0]
    builder_context = str(builder_messages[1]["content"])
    assert 'read_file({"path": "context.txt"})' in builder_context
    assert "important evidence" in builder_context


@pytest.mark.asyncio
async def test_plan_revision_then_completion(settings: Settings, storage: Storage) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan-1", name="submit_plan", arguments=_review_plan("Draft"))
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-2", name="submit_plan", arguments=_review_plan("Revised")
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Reviewed", "evidence": ["plan approved"]},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Review", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)

    await manager.decide_plan(
        run.id, PlanDecision(decision="revise", feedback="Make it narrower")
    )
    async with asyncio.timeout(3):
        while (  # noqa: ASYNC110
            storage.get_run(run.id).state is not RunState.AWAITING_PLAN_APPROVAL
            or storage.get_run(run.id).plan is None
            or storage.get_run(run.id).plan.summary != "Revised"
        ):
            await asyncio.sleep(0.01)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    assert completed.plan is not None and completed.plan.summary == "Revised"


@pytest.mark.asyncio
async def test_verifier_failure_triggers_one_repair_cycle(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create",
                        name="create_file",
                        arguments={"path": "result.txt", "content": "draft\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-1",
                        name="finish",
                        arguments={"summary": "Draft", "evidence": ["file created"]},
                    )
                ]
            ),
            _verification("fail", "The value is still a draft."),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="repair",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "--- a/result.txt\n+++ b/result.txt\n"
                                "@@ -1 +1 @@\n-draft\n+final\n"
                            )
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-2",
                        name="finish",
                        arguments={"summary": "Final", "evidence": ["file repaired"]},
                    )
                ]
            ),
            _verification("pass", "The repaired value satisfies the plan."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create a final value")
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    assert completed.repair_cycles == 1
    assert (settings.workspace / "result.txt").read_text() == "final\n"
    assert completed.plan is not None
    assert completed.plan.acceptance_checks[0].status.value == "passed"
    repair = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.REPAIR_STARTED
    )
    assert repair.payload["cycle"] == 1
    assert repair.payload["verdict"] == "fail"


@pytest.mark.asyncio
async def test_repair_cannot_reuse_a_passing_check_from_before_the_edit(
    settings: Settings, storage: Storage
) -> None:
    check = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('result.txt').exists()",
    ]
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_plan_arguments(check))
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create",
                        name="create_file",
                        arguments={"path": "result.txt", "content": "draft\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="check-1", name="run_command", arguments={"argv": check})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-1",
                        name="finish",
                        arguments={"summary": "Draft", "evidence": ["initial check"]},
                    )
                ]
            ),
            _verification("fail", "The value is still a draft."),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="repair",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "--- a/result.txt\n+++ b/result.txt\n"
                                "@@ -1 +1 @@\n-draft\n+final\n"
                            )
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-stale",
                        name="finish",
                        arguments={"summary": "Final", "evidence": ["old check"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="check-2", name="run_command", arguments={"argv": check})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-2",
                        name="finish",
                        arguments={"summary": "Final", "evidence": ["fresh check"]},
                    )
                ]
            ),
            _verification("pass", "The repaired value and fresh check satisfy the plan."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create a final value with current evidence")
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert (settings.workspace / "result.txt").read_text() == "final\n"
    assert completed.plan is not None
    assert completed.plan.acceptance_checks[0].status.value == "passed"
    assert any(
        "need fresh passing evidence" in (message.get("content") or "")
        for request, _tools in provider.requests
        for message in request
        if message.get("role") == "tool"
    )
    check_events = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["name"] == "run_command"
    ]
    assert len(check_events) == 2


@pytest.mark.asyncio
async def test_verifier_failure_at_repair_limit_fails_run(
    settings: Settings, storage: Storage
) -> None:
    limited = replace(settings, max_repair_cycles=0)
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Done", "evidence": ["claimed"]},
                    )
                ]
            ),
            _verification("inconclusive", "Evidence is insufficient."),
        ]
    )
    manager = AgentManager(limited, storage, provider)
    run = await manager.start_run("Prove the result")
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.FAILED
    assert "allowed repair cycles" in (completed.error or "")


@pytest.mark.asyncio
async def test_three_identical_tool_failures_stop_builder(
    settings: Settings, storage: Storage
) -> None:
    repeated = ModelResponse(
        tool_calls=[ToolCall(id="bad", name="missing_tool", arguments={"path": "x"})]
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            repeated,
            repeated,
            repeated,
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Try a missing tool", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.FAILED
    assert "failed three times" in (completed.error or "")


@pytest.mark.asyncio
async def test_approved_unknown_command_runs_once(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="python",
                        name="run_command",
                        arguments={"argv": ["python3", "-c", "print('approved')"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Ran", "evidence": ["approved output"]},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Run Python", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    await manager.decide_action(run.id, approved=True)

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    outputs = [
        event.payload["result"]["output"]
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["name"] == "run_command"
    ]
    assert outputs == ["approved\n"]


@pytest.mark.asyncio
async def test_shutdown_resume_plan_then_rollback(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(settings, storage, ScriptedProvider([
        ModelResponse(
            tool_calls=[ToolCall(id="plan", name="submit_plan", arguments=_review_plan())]
        )
    ]))
    run = await first.start_run("Resume me", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await first.shutdown()
    interrupted = storage.get_run(run.id)
    assert interrupted.state is RunState.INTERRUPTED
    assert interrupted.interrupted_from is RunState.AWAITING_PLAN_APPROVAL

    second = AgentManager(
        settings,
        storage,
        ScriptedProvider([
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Resumed", "evidence": ["approved"]},
                    )
                ]
            )
        ]),
    )
    await second.resume(run.id)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await second.decide_plan(run.id, PlanDecision(decision="approve"))
    completed = await second.wait(run.id)
    assert completed.state is RunState.SUCCEEDED

    rollback = await second.rollback(run.id)
    assert rollback.conflicts == []
    assert storage.get_run(run.id).state is RunState.ROLLED_BACK
    resumed = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.RUN_RESUMED
    )
    assert resumed.payload["strategy"] == "await_plan_approval"


@pytest.mark.asyncio
async def test_resume_closes_an_incomplete_tool_call_without_replaying_it(
    settings: Settings, storage: Storage
) -> None:
    interrupted_call = ModelResponse(
        tool_calls=[
            ToolCall(
                id="unknown-side-effect",
                name="create_file",
                arguments={"path": "must-not-be-replayed.txt", "content": "unsafe\n"},
            )
        ]
    ).as_assistant_message()
    run = RunRecord(
        id="resume-incomplete-tool",
        task="Recover without guessing whether the last action ran",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.EXECUTING,
        verifier_enabled=False,
        plan=TaskPlan.model_validate(_review_plan()),
        plan_approved=True,
        messages=[interrupted_call],
    )
    storage.create_run(run)
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={
                            "summary": "Recovered after inspection",
                            "evidence": ["no action replayed"],
                        },
                    )
                ]
            )
        ]
    )
    manager = AgentManager(settings, storage, provider)

    await manager.resume(run.id)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert not (settings.workspace / "must-not-be-replayed.txt").exists()
    resumed = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.RUN_RESUMED
    )
    assert resumed.payload == {
        "interrupted_from": "executing",
        "strategy": "inspect_before_execution",
        "incomplete_tool_calls_repaired": 1,
    }
    first_request = provider.requests[0][0]
    synthetic = next(message for message in first_request if message.get("role") == "tool")
    assert "stopped before this tool call completed" in synthetic["content"]


@pytest.mark.asyncio
async def test_resume_preserves_persisted_fast_path_decision(
    settings: Settings, storage: Storage
) -> None:
    plan = TaskPlan.model_validate(
        {
            "summary": "Record one low-risk note",
            "steps": [{"id": "note", "title": "Record the note"}],
            "acceptance_checks": [{"id": "review", "label": "Review the note"}],
            "impacted_files": ["note.txt"],
        }
    )
    run = RunRecord(
        id="resume-fast-path",
        task="Record note.txt",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        verifier_enabled=False,
        plan=plan,
        plan_gate=PlanGate(
            decision="auto_approved",
            risk="low",
            reasons=["Explicit single-file scope"],
        ),
    )
    storage.create_run(run)
    manager = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"summary": "Resumed", "evidence": ["plan"]},
                        )
                    ]
                )
            ]
        ),
    )

    await manager.resume(run.id)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    state_events = [
        event.payload["state"]
        for event in storage.get_events(run.id)
        if event.type is EventType.STATE_CHANGED
    ]
    assert RunState.AWAITING_PLAN_APPROVAL.value not in state_events


@pytest.mark.asyncio
async def test_cancel_waiting_run(settings: Settings, storage: Storage) -> None:
    manager = AgentManager(
        settings,
        storage,
        ScriptedProvider([
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            )
        ]),
    )
    run = await manager.start_run("Cancel me")
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)

    cancelled = await manager.cancel(run.id)
    assert cancelled.state is RunState.CANCELLED
    assert (await manager.cancel(run.id)).state is RunState.CANCELLED


@pytest.mark.asyncio
async def test_planner_prose_only_fails_and_redacts_secret(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [ModelResponse(content="thinking"), ModelResponse(content=f"leak {settings.api_key}")]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Never submit a plan")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert "did not submit" in (completed.error or "")
    messages = [
        event.payload.get("content", "")
        for event in storage.get_events(run.id)
        if event.type is EventType.MESSAGE
    ]
    assert settings.api_key not in "".join(messages)


@pytest.mark.asyncio
async def test_verifier_reads_evidence_before_structured_verdict(
    settings: Settings, storage: Storage
) -> None:
    (settings.workspace / "evidence.txt").write_text("verified\n")
    mixed_review = ModelResponse(
        content="I need one focused read before issuing the verdict.",
        tool_calls=[
            ToolCall(
                id="read",
                name="read_file",
                arguments={"path": "evidence.txt"},
            ),
            ToolCall(
                id="forbidden",
                name="create_file",
                arguments={"path": "should-not-exist", "content": "x"},
            ),
            ToolCall(
                id="early-verdict",
                name="submit_verification",
                arguments={
                    "verdict": "pass",
                    "summary": "Wait for the read result.",
                    "findings": [],
                },
            ),
        ],
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Ready", "evidence": ["evidence.txt"]},
                    )
                ]
            ),
            mixed_review,
            _verification("pass", "The focused read confirms the result."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Review evidence")
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    assert not (settings.workspace / "should-not-exist").exists()
    verifier_request = provider.requests[-1][0]
    assert any(
        message.get("name") == "create_file" and "read-only" in message.get("content", "")
        for message in verifier_request
    )


@pytest.mark.asyncio
async def test_public_actions_reject_invalid_run_states(
    settings: Settings, storage: Storage
) -> None:
    manager = AgentManager(settings, storage, ScriptedProvider([]))
    terminal = RunRecord(
        id="terminal",
        task="Done",
        workspace=str(settings.workspace),
        state=RunState.SUCCEEDED,
    )
    storage.create_run(terminal)

    with pytest.raises(ValueError, match="empty"):
        await manager.start_run("   ")
    with pytest.raises(InvalidRunAction, match="clarification"):
        await manager.answer_clarification("terminal", [])
    with pytest.raises(InvalidRunAction, match="plan approval"):
        await manager.decide_plan("terminal", PlanDecision(decision="approve"))
    with pytest.raises(InvalidRunAction, match="action approval"):
        await manager.decide_action("terminal", approved=True)
    with pytest.raises(InvalidRunAction, match="interrupted"):
        await manager.resume("terminal")

    active = RunRecord(
        id="active",
        task="Active",
        workspace=str(settings.workspace),
        state=RunState.EXECUTING,
    )
    storage.create_run(active)
    with pytest.raises(RunConflictError, match="active"):
        await manager.start_run("Another")
    with pytest.raises(InvalidRunAction, match="Cancel"):
        await manager.rollback("active")

    interrupted = RunRecord(
        id="interrupted",
        task="Stopped",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
    )
    storage.create_run(interrupted)
    cancelled = await manager.cancel("interrupted")
    assert cancelled.state is RunState.CANCELLED


@pytest.mark.asyncio
async def test_verifier_recovers_from_invalid_structured_report(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Ready", "evidence": ["reviewed"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="invalid", name="submit_verification", arguments={})
                ]
            ),
            _verification("pass", "The corrected report is valid."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Recover verifier output")
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    final_request = provider.requests[-1][0]
    assert any(
        "Invalid verification report" in (message.get("content") or "")
        for message in final_request
    )
