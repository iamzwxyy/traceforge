from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from traceforge.agent import AgentManager, InvalidRunAction, PlanDecision, RunConflictError
from traceforge.config import Settings
from traceforge.models import (
    ApprovalMode,
    ApprovalRequest,
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRequest,
    ConversationTurn,
    DecisionKind,
    DecisionStatus,
    EventType,
    InteractionMode,
    PlanGate,
    QuestionOption,
    ReasoningEffort,
    RunRecord,
    RunState,
    TaskPlan,
    ToolCall,
    ToolResult,
    Verdict,
    WorkspaceInstructionSnapshot,
)
from traceforge.prompts import (
    BUILDER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
)
from traceforge.proof import build_proof_pack, proof_pack_markdown
from traceforge.provider import ModelResponse, ProviderError, ScriptedProvider
from traceforge.sandbox import SandboxStatus
from traceforge.storage import SecureCheckpointError, Storage
from traceforge.streaming import contains_redactable_json_secret, redact_text


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


def _direct_response(content: str, *, call_id: str = "respond") -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="respond_to_user",
                arguments={"content": content},
            )
        ]
    )


def _question_response(
    call_id: str,
    *,
    question_id: str = "choice",
    prompt: str = "Choose one",
) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="ask_questions",
                arguments={
                    "questions": [
                        {
                            "id": question_id,
                            "prompt": prompt,
                            "options": [
                                {"id": "a", "label": "A"},
                                {"id": "b", "label": "B"},
                            ],
                        }
                    ]
                },
            )
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [InteractionMode.AGENT, InteractionMode.PLAN])
async def test_conversational_request_is_answered_without_false_workflow(
    settings: Settings,
    storage: Storage,
    mode: InteractionMode,
) -> None:
    provider = ScriptedProvider([_direct_response("你好! 今天想一起处理什么?")])
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("你好", mode=mode)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.ANSWERED
    assert completed.plan is None
    assert completed.clarification is None
    assert completed.verification is None
    assert completed.step_count == 0
    assert completed.turns[-1].outcome == "answered"
    assert completed.turns[-1].summary == "你好! 今天想一起处理什么?"
    event_types = [event.type for event in storage.get_events(run.id)]
    assert EventType.CLARIFICATION_REQUESTED not in event_types
    assert EventType.PLAN_GATED not in event_types
    assert EventType.VERIFICATION_COMPLETED not in event_types
    assert EventType.DIFF_UPDATED not in event_types
    tool_names = {
        schema["function"]["name"] for schema in (provider.requests[0][1] or [])
    }
    assert "respond_to_user" in tool_names
    assert "Simplified Chinese" in str(provider.requests[0][0][0]["content"])

    with pytest.raises(InvalidRunAction, match="no file changes"):
        await manager.rollback(run.id)


@pytest.mark.asyncio
async def test_planner_prose_correction_publishes_only_the_canonical_answer(
    settings: Settings, storage: Storage
) -> None:
    answer = "SELECT department, COUNT(*) FROM employees GROUP BY department;"
    provider = ScriptedProvider([
        ModelResponse(content=answer),
        _direct_response(answer),
    ])
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("Write the SQL query")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.ANSWERED
    events = storage.get_events(run.id)
    assert [event for event in events if event.type is EventType.MESSAGE] == []
    completed_turns = [
        event for event in events if event.type is EventType.TURN_COMPLETED
    ]
    assert len(completed_turns) == 1
    assert completed_turns[0].payload["summary"] == answer


@pytest.mark.asyncio
async def test_planner_read_progress_remains_visible_before_the_answer(
    settings: Settings, storage: Storage
) -> None:
    (settings.workspace / "architecture.txt").write_text("local core\n")
    provider = ScriptedProvider([
        ModelResponse(
            content="I will inspect the requested file.",
            tool_calls=[
                ToolCall(
                    id="read",
                    name="read_file",
                    arguments={"path": "architecture.txt"},
                )
            ],
        ),
        _direct_response("The core stays local."),
    ])
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("Inspect architecture.txt")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.ANSWERED
    messages = [
        event.payload["content"]
        for event in storage.get_events(run.id)
        if event.type is EventType.MESSAGE
    ]
    assert messages == ["I will inspect the requested file."]


@pytest.mark.asyncio
async def test_read_only_inspection_can_end_in_a_direct_answer(
    settings: Settings, storage: Storage
) -> None:
    (settings.workspace / "architecture.txt").write_text("local core\n")
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="read",
                        name="read_file",
                        arguments={"path": "architecture.txt"},
                    )
                ]
            ),
            _direct_response("这个项目把核心逻辑保持在本地。"),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("只分析 architecture.txt, 不要修改")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.ANSWERED
    assert completed.plan is None
    assert (settings.workspace / "architecture.txt").read_text() == "local core\n"
    tool_events = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
    ]
    assert [event.payload["call"]["name"] for event in tool_events] == ["read_file"]


@pytest.mark.asyncio
async def test_direct_response_must_be_a_separate_terminal_tool_call(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="premature",
                        name="respond_to_user",
                        arguments={"content": "不应提前结束。"},
                    ),
                    ToolCall(
                        id="plan",
                        name="submit_plan",
                        arguments=_plan_arguments(["python3", "-m", "pytest"]),
                    ),
                ]
            ),
            _direct_response("我需要先确认你的实际目标。", call_id="valid"),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("帮我一下")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.ANSWERED
    assert completed.plan is None
    assert len(provider.requests) == 2
    rejected = [
        message
        for message in completed.messages
        if message.get("role") == "tool"
        and json.loads(str(message.get("content"))).get("ok") is False
    ]
    assert {message["name"] for message in rejected} == {
        "respond_to_user",
        "submit_plan",
    }


@pytest.mark.asyncio
async def test_answered_turn_supports_follow_up_and_redacts_credentials(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            _direct_response(f"不会泄露 {settings.api_key}", call_id="first"),
            _direct_response("可以, 我们继续。", call_id="second"),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("你好")
    first = await manager.wait(run.id)
    assert first.state is RunState.ANSWERED
    assert settings.api_key not in first.model_dump_json()
    assert first.turns[-1].summary == redact_text(
        f"不会泄露 {settings.api_key}", api_key=settings.api_key
    )

    await manager.follow_up(run.id, "继续聊聊", mode=InteractionMode.AGENT)
    second = await manager.wait(run.id)

    assert second.state is RunState.ANSWERED
    assert [turn.outcome for turn in second.turns] == ["answered", "answered"]
    assert second.turns[-1].summary == "可以, 我们继续。"


def test_all_model_roles_require_simplified_chinese_user_facing_text() -> None:
    for prompt in (PLANNER_SYSTEM_PROMPT, BUILDER_SYSTEM_PROMPT, VERIFIER_SYSTEM_PROMPT):
        assert "Simplified Chinese" in prompt


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
                        arguments={"summary": "Created result.txt"},
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
    run = await manager.start_run(
        "Create result.txt after clarifying the format", mode=InteractionMode.PLAN
    )

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
async def test_agent_mode_continues_without_plan_approval_but_stays_visible(
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
                        arguments={"summary": "Created note"},
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
    assert completed.plan_gate.decision == "agent_continues"
    state_events = [
        event.payload["state"]
        for event in storage.get_events(run.id)
        if event.type is EventType.STATE_CHANGED
    ]
    assert RunState.AWAITING_PLAN_APPROVAL.value not in state_events
    assert EventType.PLAN_GATED in [event.type for event in storage.get_events(run.id)]


@pytest.mark.asyncio
async def test_plan_mode_always_pauses_for_review_even_when_low_risk(
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
                            "summary": "Review one note",
                            "approach": "Inspect the existing note and preserve its format.",
                            "steps": [{"id": "review", "title": "Review the note"}],
                            "acceptance_checks": [
                                {"id": "checked", "label": "The note is reviewed"}
                            ],
                            "impacted_files": ["note.txt"],
                        },
                    )
                ]
            )
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Review note.txt", verifier_enabled=False, mode=InteractionMode.PLAN
    )

    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    waiting = storage.get_run(run.id)

    assert waiting.plan_gate is not None
    assert waiting.plan_gate.decision == "approval_required"
    assert waiting.plan is not None
    assert "# Implementation plan" in waiting.plan.markdown
    assert "## Approach" in waiting.plan.markdown
    assert "## Validation" in waiting.plan.markdown
    await manager.cancel(run.id)


@pytest.mark.asyncio
async def test_follow_up_continues_the_same_task_with_prior_turn_context(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan-1", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-1",
                        name="finish",
                        arguments={
                            "summary": "Reviewed the first request",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan-2", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-2",
                        name="finish",
                        arguments={
                            "summary": "Applied the follow-up review",
                        },
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    first = await manager.start_run("Review the current behavior", verifier_enabled=False)
    first_completed = await manager.wait(first.id)
    assert first_completed.state is RunState.SUCCEEDED

    continued = await manager.follow_up(
        first.id, "Now focus on the edge case", mode=InteractionMode.AGENT
    )
    assert continued.id == first.id
    second_completed = await manager.wait(first.id)

    assert second_completed.state is RunState.SUCCEEDED
    assert second_completed.task == "Review the current behavior"
    assert [turn.request for turn in second_completed.turns] == [
        "Review the current behavior",
        "Now focus on the edge case",
    ]
    assert [turn.outcome for turn in second_completed.turns] == [
        "succeeded",
        "succeeded",
    ]
    second_planner_context = str(provider.requests[2][0][1]["content"])
    assert "Earlier turns in this same task" in second_planner_context
    assert "Reviewed the first request" in second_planner_context
    event_types = [event.type for event in storage.get_events(first.id)]
    assert event_types.count(EventType.TURN_STARTED) == 2
    assert event_types.count(EventType.TURN_COMPLETED) == 2


@pytest.mark.asyncio
async def test_rollback_successor_uses_fresh_snapshot_boundary_and_lineage(
    settings: Settings, storage: Storage
) -> None:
    note = settings.workspace / "note.txt"
    note.write_text("base\n")
    plan = {
        "summary": "Update the note",
        "steps": [{"id": "update", "title": "Update note.txt"}],
        "acceptance_checks": [{"id": "review", "label": "Review note.txt"}],
        "impacted_files": ["note.txt"],
    }
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="plan-1", name="submit_plan", arguments=plan)]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="patch-1",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "--- a/note.txt\n+++ b/note.txt\n"
                                "@@ -1 +1 @@\n-base\n+first agent edit\n"
                            )
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-1",
                        name="finish",
                        arguments={"summary": "Applied the first edit"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="plan-2", name="submit_plan", arguments=plan)]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="patch-2",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "--- a/note.txt\n+++ b/note.txt\n"
                                "@@ -1 +1 @@\n-user edit after rollback\n+successor edit\n"
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
                        arguments={"summary": "Applied the successor edit"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    first = await manager.start_run("Apply the first edit", verifier_enabled=False)
    assert (await manager.wait(first.id)).state is RunState.SUCCEEDED
    assert note.read_text() == "first agent edit\n"
    first_rollback = await manager.rollback(first.id)
    assert first_rollback.restored == ["note.txt"]
    assert note.read_text() == "base\n"

    note.write_text("user edit after rollback\n")
    rollback_event_count = sum(
        event.type is EventType.ROLLBACK_COMPLETED
        for event in storage.get_events(first.id)
    )
    exact_rollback_retry = await manager.rollback(first.id)
    assert exact_rollback_retry == first_rollback
    assert note.read_text() == "user edit after rollback\n"
    assert sum(
        event.type is EventType.ROLLBACK_COMPLETED
        for event in storage.get_events(first.id)
    ) == rollback_event_count

    successor = await manager.continue_after_rollback(
        first.id, "Apply a different edit after rollback"
    )
    assert successor.id != first.id
    assert successor.project_id == first.project_id
    assert storage.get_parent_run_id(successor.id) == first.id
    assert (await manager.wait(successor.id)).state is RunState.SUCCEEDED
    assert note.read_text() == "successor edit\n"

    planner_context = str(provider.requests[3][0][1]["content"])
    assert "rolled-back predecessor" in planner_context
    assert "current workspace is authoritative" in planner_context
    assert "Applied the first edit" in planner_context
    started = next(
        event
        for event in storage.get_events(successor.id)
        if event.type is EventType.TURN_STARTED
    )
    assert started.payload["continued_from_run_id"] == first.id

    exact_retry = await manager.continue_after_rollback(
        first.id, "Apply a different edit after rollback"
    )
    assert exact_retry.id == successor.id
    with pytest.raises(InvalidRunAction, match="already continued"):
        await manager.continue_after_rollback(first.id, "Start a conflicting branch")

    successor_rollback = await manager.rollback(successor.id)
    assert successor_rollback.restored == ["note.txt"]
    assert successor_rollback.conflicts == []
    assert note.read_text() == "user edit after rollback\n"


@pytest.mark.asyncio
async def test_rollback_abandons_every_interrupted_human_decision_before_files(
    settings: Settings, storage: Storage
) -> None:
    manager = AgentManager(settings, storage, ScriptedProvider([]))
    clarification = ClarificationRequest(
        questions=[
            ClarificationQuestion(
                id="scope",
                prompt="Which scope?",
                options=[
                    QuestionOption(id="small", label="Small"),
                    QuestionOption(id="large", label="Large"),
                ],
            )
        ]
    )
    plan = TaskPlan.model_validate(_review_plan())
    action = ApprovalRequest(
        id="rollback-action-request",
        tool_call=ToolCall(
            id="rollback-command",
            name="run_command",
            arguments={"argv": ["python", "app.py"]},
        ),
        summary="Run app.py",
        reason="Manual approval is required",
        risk="elevated",
        approval_mode=ApprovalMode.MANUAL,
        policy_decision="ask",
    )
    cases = [
        (
            "rollback-clarification",
            DecisionKind.CLARIFICATION,
            RunState.PLANNING,
            RunState.AWAITING_CLARIFICATION,
            clarification.model_dump(mode="json"),
            EventType.CLARIFICATION_REQUESTED,
        ),
        (
            "rollback-plan",
            DecisionKind.PLAN,
            RunState.PLANNING,
            RunState.AWAITING_PLAN_APPROVAL,
            plan.model_dump(mode="json"),
            EventType.PLAN_UPDATED,
        ),
        (
            "rollback-action",
            DecisionKind.ACTION,
            RunState.EXECUTING,
            RunState.AWAITING_ACTION_APPROVAL,
            action.model_dump(mode="json"),
            EventType.APPROVAL_REQUESTED,
        ),
    ]

    for run_id, kind, previous, waiting_state, subject, event_type in cases:
        run = RunRecord(
            id=run_id,
            task="Rollback an interrupted decision",
            workspace=str(settings.workspace),
            state=previous,
            clarification=clarification if kind is DecisionKind.CLARIFICATION else None,
            plan=plan if kind is DecisionKind.PLAN else None,
            pending_approval=action if kind is DecisionKind.ACTION else None,
        )
        storage.create_run(run)
        run.state = waiting_state
        request_id = action.id if kind is DecisionKind.ACTION else f"{run_id}-request"
        storage.open_decision(
            run,
            previous_state=previous,
            request_id=request_id,
            kind=kind,
            turn_index=1,
            subject=subject,
            requested_event_type=event_type,
            requested_payload=subject,
        )
        accepted_payload = (
            {"answers": [{"question_id": "scope", "option_id": "small"}]}
            if kind is DecisionKind.CLARIFICATION
            else (
                {"decision": "approve", "feedback": ""}
                if kind is DecisionKind.PLAN
                else {"approved": True}
            )
        )
        storage.accept_decision(run_id, request_id, kind, accepted_payload)
        interrupted = storage.get_run(run_id)
        interrupted.interrupted_from = waiting_state
        interrupted.state = RunState.INTERRUPTED
        storage.save_run(interrupted)

        await manager.rollback(run_id)

        rolled_back = storage.get_run(run_id)
        assert rolled_back.state is RunState.ROLLED_BACK
        assert rolled_back.pending_approval is None
        assert rolled_back.clarification is None
        assert storage.get_active_decision(run_id) is None
        assert storage.get_decision(run_id, request_id).status is DecisionStatus.ABANDONED
        if kind is DecisionKind.CLARIFICATION:
            stale_call = manager.answer_clarification(
                run_id,
                [ClarificationAnswer(question_id="scope", option_id="small")],
                request_id=request_id,
            )
        elif kind is DecisionKind.PLAN:
            stale_call = manager.decide_plan(
                run_id,
                PlanDecision(decision="approve"),
                request_id=request_id,
            )
        else:
            stale_call = manager.decide_action(run_id, request_id, approved=True)
        with pytest.raises(InvalidRunAction, match="abandoned"):
            await stale_call


@pytest.mark.asyncio
async def test_rollback_file_failure_cannot_revive_an_old_accepted_decision(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarification = ClarificationRequest(
        questions=[
            ClarificationQuestion(
                id="scope",
                prompt="Which scope?",
                options=[
                    QuestionOption(id="small", label="Small"),
                    QuestionOption(id="large", label="Large"),
                ],
            )
        ]
    )
    run = RunRecord(
        id="rollback-file-failure",
        task="Rollback before consuming an answer",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
        clarification=clarification,
    )
    storage.create_run(run)
    run.state = RunState.AWAITING_CLARIFICATION
    storage.open_decision(
        run,
        previous_state=RunState.PLANNING,
        request_id="clarification-before-rollback",
        kind=DecisionKind.CLARIFICATION,
        turn_index=1,
        subject=clarification.model_dump(mode="json"),
        requested_event_type=EventType.CLARIFICATION_REQUESTED,
        requested_payload=clarification.model_dump(mode="json"),
    )
    storage.accept_decision(
        run.id,
        "clarification-before-rollback",
        DecisionKind.CLARIFICATION,
        {"answers": [{"question_id": "scope", "option_id": "small"}]},
    )
    interrupted = storage.get_run(run.id)
    interrupted.state = RunState.INTERRUPTED
    interrupted.interrupted_from = RunState.AWAITING_CLARIFICATION
    storage.save_run(interrupted)
    manager = AgentManager(settings, storage, ScriptedProvider([]))

    def fail_file_rollback(_run_id: str) -> None:
        raise RuntimeError("snapshot storage unavailable")

    monkeypatch.setattr(manager.workspace, "rollback", fail_file_rollback)
    with pytest.raises(RuntimeError, match="snapshot storage unavailable"):
        await manager.rollback(run.id)

    unchanged = storage.get_run(run.id)
    assert unchanged.state is RunState.INTERRUPTED
    assert unchanged.clarification is None
    assert storage.get_active_decision(run.id) is None
    assert storage.get_decision(
        run.id, "clarification-before-rollback"
    ).status is DecisionStatus.ABANDONED
    with pytest.raises(InvalidRunAction, match="abandoned"):
        await manager.answer_clarification(
            run.id,
            [ClarificationAnswer(question_id="scope", option_id="small")],
            request_id="clarification-before-rollback",
        )


@pytest.mark.asyncio
async def test_rollback_cannot_race_a_newly_resumed_worker(
    settings: Settings, storage: Storage
) -> None:
    blocker = asyncio.Event()

    class BlockingProvider:
        async def complete(self, messages, tools=None, **_kwargs) -> ModelResponse:
            await blocker.wait()
            raise AssertionError("unreachable")

    run = RunRecord(
        id="resume-rollback-race",
        task="Resume or rollback, never both",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        turns=[ConversationTurn(index=1, request="Resume or rollback, never both")],
    )
    storage.create_run(
        run,
        instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
    )
    manager = AgentManager(settings, storage, BlockingProvider())

    await manager.resume(run.id)
    with pytest.raises(RunConflictError, match="already active"):
        await manager.rollback(run.id)

    await manager.shutdown()
    assert storage.get_run(run.id).state is RunState.INTERRUPTED


@pytest.mark.asyncio
async def test_success_freezes_proof_after_the_completion_event(
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
                        arguments={"summary": "Reviewed"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Review the workspace", verifier_enabled=False)

    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    frozen = storage.get_proof_pack(run.id, 1)

    assert completed.state is RunState.SUCCEEDED
    assert events[-1].type is EventType.RUN_COMPLETED
    assert frozen is not None
    assert frozen.event_count == len(events)
    assert frozen.turns[-1].outcome == "succeeded"


@pytest.mark.asyncio
async def test_atomic_success_failure_leaves_no_half_terminal_boundary(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
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
                        arguments={"summary": "Reviewed before the follow-up"},
                    )
                ]
            ),
        ]
    )
    def fail_proof_insert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("atomic proof insert failed")

    monkeypatch.setattr(storage, "_save_proof_pack_if_absent_locked", fail_proof_insert)
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Review the workspace", verifier_enabled=False)
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)

    assert completed.state is RunState.FAILED
    assert completed.turns[-1].outcome == "failed"
    assert completed.plan is not None
    assert completed.plan.steps[-1].status != "completed"
    assert storage.get_proof_pack(run.id) is None
    assert not any(
        event.type is EventType.STATE_CHANGED
        and event.payload.get("state") == RunState.SUCCEEDED.value
        for event in events
    )
    assert not any(
        event.type is EventType.TURN_COMPLETED
        and event.payload.get("outcome") == "succeeded"
        for event in events
    )
    assert not any(
        event.type is EventType.RUN_COMPLETED
        and event.payload.get("state") == RunState.SUCCEEDED.value
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["follow_up", "rollback"])
async def test_success_fallback_freeze_failure_has_zero_lifecycle_mutation(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    run = RunRecord(
        id=f"fallback-freeze-{action}",
        task="Preserve the successful evidence",
        workspace=str(settings.workspace),
        state=RunState.SUCCEEDED,
        turns=[
            ConversationTurn(
                index=1,
                request="Preserve the successful evidence",
                outcome="succeeded",
                summary="Evidence ready",
            )
        ],
    )
    storage.create_run(run)
    storage.append_event(
        run.id,
        EventType.TURN_COMPLETED,
        {"index": 1, "outcome": "succeeded", "summary": "Evidence ready"},
    )
    storage.append_event(
        run.id,
        EventType.RUN_COMPLETED,
        {"state": RunState.SUCCEEDED.value, "diff": ""},
    )
    manager = AgentManager(settings, storage, ScriptedProvider([]))
    before_run = storage.get_run(run.id).model_dump_json()
    before_events = [event.model_dump_json() for event in storage.get_events(run.id)]

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("proof storage unavailable")

    monkeypatch.setattr(storage, "save_proof_pack_if_absent", fail_save)

    with pytest.raises(RuntimeError, match="proof storage unavailable"):
        if action == "follow_up":
            await manager.follow_up(run.id, "Continue only after freezing")
        else:
            await manager.rollback(run.id)

    assert storage.get_run(run.id).model_dump_json() == before_run
    assert [event.model_dump_json() for event in storage.get_events(run.id)] == before_events
    assert storage.get_proof_pack(run.id) is None


@pytest.mark.asyncio
async def test_gets_and_follow_up_wait_for_atomic_success_finalization(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
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
                        arguments={"summary": "Finalized atomically"},
                    )
                ]
            ),
            _direct_response("The frozen success is still available."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    entered_finalization = asyncio.Event()
    release_finalization = asyncio.Event()
    original_cleanup = manager._prepare_terminal_cleanup

    async def pause_before_commit(run: RunRecord, intended_state: RunState) -> bool:
        ready = await original_cleanup(run, intended_state)
        if ready and intended_state is RunState.SUCCEEDED:
            entered_finalization.set()
            await release_finalization.wait()
        return ready

    monkeypatch.setattr(manager, "_prepare_terminal_cleanup", pause_before_commit)
    run = await manager.start_run("Review atomically", verifier_enabled=False)
    await asyncio.wait_for(entered_finalization.wait(), timeout=3)

    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.VERIFYING
    assert persisted.turns[-1].outcome == "in_progress"
    assert storage.get_proof_pack(run.id) is None
    assert not any(
        event.type is EventType.RUN_COMPLETED
        and event.payload.get("state") == RunState.SUCCEEDED.value
        for event in storage.get_events(run.id)
    )

    first_get = asyncio.create_task(manager.get_proof_pack(run.id))
    second_get = asyncio.create_task(manager.get_proof_pack(run.id, 1))
    follow_up = asyncio.create_task(
        manager.follow_up(run.id, "Describe the completed review")
    )
    await asyncio.sleep(0)
    assert not first_get.done()
    assert not second_get.done()
    assert not follow_up.done()

    release_finalization.set()
    first_result, second_result, continued = await asyncio.gather(
        first_get, second_get, follow_up
    )
    first_pack = first_result[1]
    second_pack = second_result[1]

    assert first_pack is not None
    assert second_pack is not None
    assert first_pack.artifact_sha256 == second_pack.artifact_sha256
    assert continued.turns[-1].index == 2
    answered = await manager.wait(run.id)
    assert answered.state is RunState.ANSWERED
    assert storage.get_proof_pack(run.id, 1) == first_pack


@pytest.mark.asyncio
async def test_get_and_rollback_wait_for_atomic_success_finalization(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
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
                        arguments={"summary": "Ready before rollback"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    entered_finalization = asyncio.Event()
    release_finalization = asyncio.Event()
    original_cleanup = manager._prepare_terminal_cleanup

    async def pause_before_commit(run: RunRecord, intended_state: RunState) -> bool:
        ready = await original_cleanup(run, intended_state)
        if ready and intended_state is RunState.SUCCEEDED:
            entered_finalization.set()
            await release_finalization.wait()
        return ready

    monkeypatch.setattr(manager, "_prepare_terminal_cleanup", pause_before_commit)
    run = await manager.start_run("Review before rollback", verifier_enabled=False)
    await asyncio.wait_for(entered_finalization.wait(), timeout=3)

    proof_request = asyncio.create_task(manager.get_proof_pack(run.id))
    rollback_request = asyncio.create_task(manager.rollback(run.id))
    await asyncio.sleep(0)
    assert not proof_request.done()
    assert not rollback_request.done()

    release_finalization.set()
    proof_result, rollback_result = await asyncio.gather(
        proof_request, rollback_request
    )
    pack = proof_result[1]

    assert pack is not None
    assert rollback_result.restored == []
    assert rollback_result.removed == []
    assert storage.get_run(run.id).state is RunState.ROLLED_BACK
    assert storage.get_proof_pack(run.id, 1) == pack
    assert pack.rollback.status == "not_available"


@pytest.mark.asyncio
async def test_each_turn_persists_its_actual_native_edit_files(
    settings: Settings, storage: Storage
) -> None:
    first_plan = {
        "summary": "Create the first file",
        "steps": [{"id": "create", "title": "Create a.txt"}],
        "acceptance_checks": [{"id": "review", "label": "a.txt is ready"}],
        "impacted_files": ["a.txt"],
    }
    second_plan = {
        "summary": "Update the first file and create another",
        "steps": [{"id": "update", "title": "Update both files"}],
        "acceptance_checks": [{"id": "review", "label": "Both files are ready"}],
        "impacted_files": ["a.txt", "b.txt"],
    }
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan-1", name="submit_plan", arguments=first_plan)
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create-a",
                        name="create_file",
                        arguments={"path": "a.txt", "content": "one\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-1",
                        name="finish",
                        arguments={"summary": "Created a.txt"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan-2", name="submit_plan", arguments=second_plan)
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="patch-a",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "--- a/a.txt\n+++ b/a.txt\n"
                                "@@ -1 +1 @@\n-one\n+two\n"
                            )
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create-b",
                        name="create_file",
                        arguments={"path": "b.txt", "content": "new\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-2",
                        name="finish",
                        arguments={"summary": "Updated both files"},
                    )
                ]
            ),
            _direct_response("前两轮共修改了 a.txt 和 b.txt。", call_id="answer-3"),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create a.txt", verifier_enabled=False)
    await manager.wait(run.id)

    await manager.follow_up(run.id, "Update a.txt and add b.txt")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    assert [turn.changed_files for turn in completed.turns] == [
        ["a.txt"],
        ["a.txt", "b.txt"],
    ]
    completion_events = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.TURN_COMPLETED
    ]
    assert [event.payload["changed_files"] for event in completion_events] == [
        ["a.txt"],
        ["a.txt", "b.txt"],
    ]
    assert [turn.changed_files for turn in storage.get_run(run.id).turns] == [
        ["a.txt"],
        ["a.txt", "b.txt"],
    ]

    await manager.follow_up(run.id, "刚才改了哪些文件?")
    answered = await manager.wait(run.id)
    assert answered.state is RunState.ANSWERED
    assert answered.turns[-1].changed_files == []

    rollback = await manager.rollback(run.id)
    assert rollback.removed == ["a.txt", "b.txt"]
    assert storage.get_run(run.id).state is RunState.ROLLED_BACK


@pytest.mark.asyncio
async def test_partial_edit_files_survive_terminal_builder_failure(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = settings.workspace / "first.txt"
    second = settings.workspace / "second.txt"
    first.write_text("before one\n")
    second.write_text("before two\n")
    plan = {
        "summary": "Update two files",
        "steps": [{"id": "update", "title": "Update both files"}],
        "acceptance_checks": [{"id": "review", "label": "Both files are ready"}],
        "impacted_files": ["first.txt", "second.txt"],
    }
    partial_patch = ToolCall(
        id="partial",
        name="apply_patch",
        arguments={
            "patch": (
                "--- a/first.txt\n+++ b/first.txt\n"
                "@@ -1 +1 @@\n-before one\n+after one\n"
                "--- a/second.txt\n+++ b/second.txt\n"
                "@@ -1 +1 @@\n-before two\n+after two\n"
            )
        },
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="plan", name="submit_plan", arguments=plan)]
            ),
            ModelResponse(tool_calls=[partial_patch]),
            ModelResponse(tool_calls=[partial_patch.model_copy(update={"id": "retry-1"})]),
            ModelResponse(tool_calls=[partial_patch.model_copy(update={"id": "retry-2"})]),
        ]
    )
    original_write_text = Path.write_text

    def fail_second_write(path: Path, content: str, **kwargs: object) -> int:
        if path.resolve() == second.resolve():
            raise OSError("simulated second-file write failure")
        return original_write_text(path, content, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_second_write)
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Update both files", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert completed.turns[-1].changed_files == ["first.txt"]
    assert storage.get_run(run.id).turns[-1].changed_files == ["first.txt"]
    completion = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.TURN_COMPLETED
    )
    assert completion.payload["changed_files"] == ["first.txt"]
    assert EventType.DIFF_UPDATED in [
        event.type for event in storage.get_events(run.id)
    ]
    assert first.read_text() == "after one\n"
    assert second.read_text() == "before two\n"


@pytest.mark.asyncio
async def test_no_op_edit_keeps_passing_check_fresh_and_reports_no_changed_file(
    settings: Settings, storage: Storage
) -> None:
    target = settings.workspace / "result.txt"
    target.write_text("ready\n")
    check = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('result.txt').read_text() == 'ready\\n'",
    ]
    plan = _plan_arguments(check) | {"impacted_files": ["result.txt"]}
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="plan", name="submit_plan", arguments=plan)]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="check", name="run_command", arguments={"argv": check})
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="no-op",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "--- a/result.txt\n+++ b/result.txt\n"
                                "@@ -1 +1 @@\n-ready\n+ready\n"
                            )
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={
                            "summary": "Confirmed the existing file",
                        },
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Confirm result.txt", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.turns[-1].changed_files == []
    assert completed.plan is not None
    assert completed.plan.acceptance_checks[0].status.value == "passed"
    assert EventType.DIFF_UPDATED not in [
        event.type for event in storage.get_events(run.id)
    ]
    assert target.read_text() == "ready\n"


@pytest.mark.asyncio
async def test_planner_can_repair_invalid_structured_calls(
    settings: Settings, storage: Storage
) -> None:
    invalid_questions = {
        "questions": [
            {
                "id": "format",
                "prompt": "Which format?",
                "options": [{"id": "text", "label": "Text"}],
            }
        ]
    }
    invalid = {
        "summary": "Create one note",
        "steps": [{"id": "create", "title": "Create the note"}],
        "acceptance_checks": [{"id": "review", "label": "The note is correct"}],
        "impacted_files": ["note.txt"],
        "risks": [{"risk": "The model used an object instead of a string"}],
    }
    valid = {**invalid, "risks": ["The note must remain inside the workspace"]}
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="invalid-questions",
                        name="ask_questions",
                        arguments=invalid_questions,
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="invalid-plan", name="submit_plan", arguments=invalid)
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="valid-plan", name="submit_plan", arguments=valid)]
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
                        arguments={"summary": "Created note"},
                    )
                ]
            ),
            _verification("pass", "The repaired plan and note satisfy the task."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create note.txt")

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    question_correction = provider.requests[1][0]
    question_result = next(
        message
        for message in question_correction
        if message.get("tool_call_id") == "invalid-questions"
    )
    plan_correction = provider.requests[2][0]
    plan_result = next(
        message
        for message in plan_correction
        if message.get("tool_call_id") == "invalid-plan"
    )
    assert "Invalid clarification request schema" in question_result["content"]
    assert "Invalid plan schema" in plan_result["content"]
    assert "Which format?" not in question_result["content"]
    assert "The model used an object" not in plan_result["content"]
    rejected_call_ids = {
        event.payload["call"]["id"]
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["result"]["ok"] is False
    }
    assert {"invalid-questions", "invalid-plan"} <= rejected_call_ids


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
                        arguments={"summary": "Observed"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Inspect the Python environment",
        verifier_enabled=False,
        mode=InteractionMode.PLAN,
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)

    approval = storage.get_run(run.id).pending_approval
    assert approval is not None
    await manager.decide_action(run.id, approval.id, approved=False)
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
async def test_terminal_builder_and_verifier_drafts_are_not_published(
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
                content="Reviewed",
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Reviewed"},
                    )
                ],
            ),
            ModelResponse(
                content="Verified",
                tool_calls=[
                    ToolCall(
                        id="verify",
                        name="submit_verification",
                        arguments={
                            "verdict": "pass",
                            "summary": "Verified",
                            "findings": [],
                        },
                    )
                ],
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("Review the workspace")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    events = storage.get_events(run.id)
    assert [event for event in events if event.type is EventType.MESSAGE] == []
    completed_turns = [
        event for event in events if event.type is EventType.TURN_COMPLETED
    ]
    assert len(completed_turns) == 1
    assert completed_turns[0].payload["summary"] == "Reviewed"


@pytest.mark.parametrize("verifier_enabled", [False, True])
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="missing-summary"),
        pytest.param({"summary": "   "}, id="blank-summary"),
        pytest.param({"summary": 1}, id="non-string-summary"),
        pytest.param(
            {"summary": "Reviewed", "evidence": ["legacy claim"]},
            id="legacy-evidence-forbidden",
        ),
    ],
)
@pytest.mark.asyncio
async def test_builder_rejects_malformed_finish_before_verification(
    settings: Settings,
    storage: Storage,
    verifier_enabled: bool,
    arguments: dict[str, object],
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
                    ToolCall(id="malformed-finish", name="finish", arguments=arguments)
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run(
        "Review the workspace", verifier_enabled=verifier_enabled
    )
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert completed.verification is None
    correction_request = provider.requests[-1][0]
    result = json.loads(
        next(
            message["content"]
            for message in correction_request
            if message.get("tool_call_id") == "malformed-finish"
        )
    )
    assert result["ok"] is False
    assert "Invalid finish schema" in result["error"]
    events = storage.get_events(run.id)
    assert EventType.VERIFICATION_COMPLETED not in {event.type for event in events}
    assert [
        event.payload["id"]
        for event in events
        if event.type is EventType.TOOL_REQUESTED
    ] == ["malformed-finish"]
    assert [
        event.payload["call"]["id"]
        for event in events
        if event.type is EventType.TOOL_COMPLETED
    ] == ["malformed-finish"]
    assert not [event for event in events if event.type is EventType.TOOL_STARTED]
    rejected = next(
        event for event in events if event.type is EventType.TOOL_COMPLETED
    )
    assert rejected.payload["result"]["metadata"] == {
        "outcome": "protocol_rejected",
        "execution": "not_started",
    }


@pytest.mark.asyncio
async def test_builder_fails_after_three_consecutive_malformed_finish_batches(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            *[
                ModelResponse(
                    tool_calls=[
                        ToolCall(id=f"malformed-{index}", name="finish", arguments={})
                    ]
                )
                for index in range(3)
            ],
        ],
        repeat=True,
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("Reject repeated malformed finish", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert "three consecutive rejected" in (completed.error or "")
    assert len(provider.requests) == 4
    paired_ids = {
        message.get("tool_call_id")
        for message in completed.messages
        if message.get("role") == "tool"
    }
    assert {f"malformed-{index}" for index in range(3)} <= paired_ids


@pytest.mark.asyncio
async def test_successful_builder_batch_resets_consecutive_rejection_budget(
    settings: Settings, storage: Storage
) -> None:
    def malformed(call_id: str) -> ModelResponse:
        return ModelResponse(
            tool_calls=[ToolCall(id=call_id, name="finish", arguments={})]
        )

    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            malformed("malformed-1"),
            malformed("malformed-2"),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create",
                        name="create_file",
                        arguments={"path": "reset.txt", "content": "reset\n"},
                    )
                ]
            ),
            malformed("malformed-3"),
            malformed("malformed-4"),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Created reset.txt"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("Reset rejection budget", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.step_count == 1
    assert (settings.workspace / "reset.txt").read_text() == "reset\n"


@pytest.mark.parametrize("finish_position", ["first", "last"])
@pytest.mark.asyncio
async def test_builder_rejects_entire_mixed_finish_batch_and_preserves_public_progress(
    settings: Settings,
    storage: Storage,
    finish_position: str,
) -> None:
    finish = ToolCall(
        id="mixed-finish",
        name="finish",
        arguments={"summary": "Not done"},
    )
    rejected_create = ToolCall(
        id="mixed-create",
        name="create_file",
        arguments={"path": "rejected.txt", "content": "must not exist\n"},
    )
    mixed_calls = (
        [finish, rejected_create]
        if finish_position == "first"
        else [rejected_create, finish]
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ]
            ),
            ModelResponse(content="Rejected mixed progress", tool_calls=mixed_calls),
            ModelResponse(
                content="Accepted progress",
                tool_calls=[
                    ToolCall(
                        id="accepted-create",
                        name="create_file",
                        arguments={"path": "accepted.txt", "content": "created\n"},
                    )
                ],
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="valid-finish",
                        name="finish",
                        arguments={"summary": "Created accepted.txt"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("Create the accepted file", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.step_count == 1
    assert not (settings.workspace / "rejected.txt").exists()
    assert (settings.workspace / "accepted.txt").read_text() == "created\n"
    correction_request = provider.requests[2][0]
    rejected_results = {
        message["tool_call_id"]: json.loads(message["content"])
        for message in correction_request
        if message.get("tool_call_id") in {"mixed-finish", "mixed-create"}
    }
    assert set(rejected_results) == {"mixed-finish", "mixed-create"}
    assert all(result["ok"] is False for result in rejected_results.values())
    assert all(
        "No tool call in this response was executed" in result["error"]
        for result in rejected_results.values()
    )
    tool_events = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
    ]
    expected_ids = [call.id for call in mixed_calls] + ["accepted-create"]
    assert [event.payload["call"]["id"] for event in tool_events] == expected_ids
    assert all(
        event.payload["result"]["metadata"]
        == {"outcome": "protocol_rejected", "execution": "not_started"}
        for event in tool_events[:2]
    )
    requested_ids = [
        event.payload["id"]
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_REQUESTED
    ]
    assert requested_ids == expected_ids
    started_ids = [
        event.payload["id"]
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_STARTED
    ]
    assert started_ids == ["accepted-create"]
    messages = [
        event.payload["content"]
        for event in storage.get_events(run.id)
        if event.type is EventType.MESSAGE
    ]
    assert messages == ["Accepted progress"]


@pytest.mark.parametrize("verifier_enabled", [False, True])
@pytest.mark.parametrize("action_count", [1, 2], ids=["max-minus-one", "exact-max"])
@pytest.mark.asyncio
async def test_builder_action_budget_allows_finish_below_or_at_exact_limit(
    settings: Settings,
    storage: Storage,
    verifier_enabled: bool,
    action_count: int,
) -> None:
    limited = replace(settings, max_steps=2)
    responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
            ]
        ),
        *[
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id=f"create-{index}",
                        name="create_file",
                        arguments={
                            "path": f"budget-{index}.txt",
                            "content": f"{index}\n",
                        },
                    )
                ]
            )
            for index in range(action_count)
        ],
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="finish",
                    name="finish",
                    arguments={"summary": "Budget respected"},
                )
            ]
        ),
    ]
    if verifier_enabled:
        responses.append(_verification("pass", "The bounded actions satisfy the plan."))
    provider = ScriptedProvider(responses)
    manager = AgentManager(limited, storage, provider)

    run = await manager.start_run(
        "Exercise the action budget", verifier_enabled=verifier_enabled
    )
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.step_count == action_count
    assert all(
        (settings.workspace / f"budget-{index}.txt").exists()
        for index in range(action_count)
    )
    finish_tool_names = {
        schema["function"]["name"]
        for schema in (provider.requests[action_count + 1][1] or [])
    }
    if action_count == limited.max_steps:
        assert finish_tool_names == {"finish"}
    else:
        assert {"finish", "create_file", "update_plan"} <= finish_tool_names


@pytest.mark.asyncio
async def test_exact_action_budget_verifier_failure_stops_without_phantom_repair(
    settings: Settings, storage: Storage
) -> None:
    limited = replace(settings, max_steps=1, max_repair_cycles=2)
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
                        id="consume-budget",
                        name="create_file",
                        arguments={"path": "draft.txt", "content": "draft\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Draft created"},
                    )
                ]
            ),
            _verification("fail", "The draft does not satisfy the task."),
            _verification("pass", "This response must never be requested."),
        ]
    )
    manager = AgentManager(limited, storage, provider)

    run = await manager.start_run("Produce a finished result")
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert completed.step_count == 1
    assert completed.repair_cycles == 0
    assert "total non-terminal tool action budget" in (completed.error or "")
    assert len(provider.requests) == 4
    events = storage.get_events(run.id)
    assert sum(event.type is EventType.VERIFICATION_COMPLETED for event in events) == 1
    assert EventType.REPAIR_STARTED not in {event.type for event in events}


@pytest.mark.asyncio
async def test_builder_rejects_over_budget_batch_before_executing_any_call(
    settings: Settings, storage: Storage
) -> None:
    limited = replace(settings, max_steps=2)
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
                        id="first",
                        name="create_file",
                        arguments={"path": "first.txt", "content": "first\n"},
                    )
                ]
            ),
            ModelResponse(
                content="Rejected over-budget progress",
                tool_calls=[
                    ToolCall(
                        id="overflow-a",
                        name="create_file",
                        arguments={"path": "overflow-a.txt", "content": "a\n"},
                    ),
                    ToolCall(
                        id="overflow-command",
                        name="run_command",
                        arguments={"argv": ["python3", "-V"]},
                    ),
                ],
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="second",
                        name="create_file",
                        arguments={"path": "second.txt", "content": "second\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Created two files"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(limited, storage, provider)

    run = await manager.start_run("Stay within two actions", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.step_count == 2
    assert (settings.workspace / "first.txt").exists()
    assert (settings.workspace / "second.txt").exists()
    assert not (settings.workspace / "overflow-a.txt").exists()
    correction_request = provider.requests[3][0]
    overflow_results = {
        message["tool_call_id"]: json.loads(message["content"])
        for message in correction_request
        if message.get("tool_call_id") in {"overflow-a", "overflow-command"}
    }
    assert set(overflow_results) == {"overflow-a", "overflow-command"}
    assert all(result["ok"] is False for result in overflow_results.values())
    assert all(
        "1 remaining, 2 requested" in result["error"]
        for result in overflow_results.values()
    )
    events = storage.get_events(run.id)
    completed_events = [
        event for event in events if event.type is EventType.TOOL_COMPLETED
    ]
    assert [event.payload["call"]["id"] for event in completed_events] == [
        "first",
        "overflow-a",
        "overflow-command",
        "second",
    ]
    assert all(
        event.payload["result"]["metadata"]
        == {"outcome": "protocol_rejected", "execution": "not_started"}
        for event in completed_events[1:3]
    )
    assert [
        event.payload["id"]
        for event in events
        if event.type is EventType.TOOL_STARTED
    ] == ["first", "second"]
    pack = build_proof_pack(completed, storage)
    assert pack.command_sandbox.not_executed_commands == 1
    assert [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.MESSAGE
    ] == []


@pytest.mark.asyncio
async def test_builder_fails_after_three_consecutive_over_budget_batches(
    settings: Settings, storage: Storage
) -> None:
    limited = replace(settings, max_steps=1)
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
                        id="accepted",
                        name="create_file",
                        arguments={"path": "accepted.txt", "content": "accepted\n"},
                    )
                ]
            ),
            *[
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"overflow-{index}",
                            name="create_file",
                            arguments={
                                "path": f"overflow-{index}.txt",
                                "content": "rejected\n",
                            },
                        )
                    ]
                )
                for index in range(3)
            ],
        ],
        repeat=True,
    )
    manager = AgentManager(limited, storage, provider)

    run = await manager.start_run("Reject repeated overflow", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert completed.step_count == 1
    assert "three consecutive rejected" in (completed.error or "")
    assert len(provider.requests) == 5
    assert (settings.workspace / "accepted.txt").exists()
    assert all(
        not (settings.workspace / f"overflow-{index}.txt").exists()
        for index in range(3)
    )
    paired_ids = {
        message.get("tool_call_id")
        for message in completed.messages
        if message.get("role") == "tool"
    }
    assert {f"overflow-{index}" for index in range(3)} <= paired_ids


@pytest.mark.asyncio
async def test_rejected_finish_does_not_consume_budget_needed_for_command_check(
    settings: Settings, storage: Storage
) -> None:
    limited = replace(settings, max_steps=2)
    check = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('checked.txt').read_text() == 'ready\\n'",
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
                        arguments={"path": "checked.txt", "content": "ready\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="premature-finish",
                        name="finish",
                        arguments={"summary": "Ready"},
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
                        arguments={"summary": "Created and checked"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(limited, storage, provider)

    run = await manager.start_run("Create and check the file", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.step_count == 2
    assert completed.plan is not None
    assert completed.plan.acceptance_checks[0].status.value == "passed"
    correction_request = provider.requests[3][0]
    premature_result = json.loads(
        next(
            message["content"]
            for message in correction_request
            if message.get("tool_call_id") == "premature-finish"
        )
    )
    assert premature_result["ok"] is False
    assert "fresh passing evidence" in premature_result["error"]
    final_tool_names = {
        schema["function"]["name"] for schema in (provider.requests[4][1] or [])
    }
    assert final_tool_names == {"finish"}


@pytest.mark.asyncio
async def test_finish_fails_immediately_when_budget_cannot_run_missing_command_check(
    settings: Settings, storage: Storage
) -> None:
    limited = replace(settings, max_steps=1)
    check = ["python3", "-c", "raise SystemExit(0)"]
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
                        id="consume-budget",
                        name="create_file",
                        arguments={"path": "created.txt", "content": "created\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="blocked-finish",
                        name="finish",
                        arguments={"summary": "Not checked"},
                    )
                ]
            ),
        ],
        repeat=True,
    )
    manager = AgentManager(limited, storage, provider)

    run = await manager.start_run("Do not finish without the command", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert completed.step_count == 1
    assert "before all command checks" in (completed.error or "")
    assert len(provider.requests) == 3
    blocked_result = json.loads(
        next(
            message["content"]
            for message in completed.messages
            if message.get("tool_call_id") == "blocked-finish"
        )
    )
    assert blocked_result["ok"] is False
    assert "fresh passing evidence" in blocked_result["error"]


@pytest.mark.asyncio
async def test_builder_publishes_only_progress_backed_by_accepted_tools(
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
                content="Rejected mixed draft",
                tool_calls=[
                    ToolCall(
                        id="accepted-before-unknown",
                        name="update_plan",
                        arguments={
                            "updates": [{"id": "review", "status": "in_progress"}]
                        },
                    ),
                    ToolCall(
                        id="unknown",
                        name="respond_to_user",
                        arguments={"content": "Reviewed"},
                    )
                ],
            ),
            ModelResponse(
                content="Rejected malformed draft",
                tool_calls=[
                    ToolCall(id="malformed", name="update_plan", arguments={})
                ],
            ),
            ModelResponse(
                content="Accepted progress",
                tool_calls=[
                    ToolCall(
                        id="update",
                        name="update_plan",
                        arguments={
                            "updates": [{"id": "review", "status": "in_progress"}]
                        },
                    )
                ],
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Reviewed"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("Review the workspace", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    messages = [
        event.payload["content"]
        for event in storage.get_events(run.id)
        if event.type is EventType.MESSAGE
    ]
    assert messages == ["Accepted progress"]


@pytest.mark.asyncio
async def test_reasoning_effort_is_frozen_across_planner_builder_and_verifier(
    settings: Settings, storage: Storage
) -> None:
    sentinel = "PRIVATE-REASONING-MUST-NOT-LEAK"
    provider = ScriptedProvider(
        [
            ModelResponse(
                reasoning_content=sentinel,
                tool_calls=[
                    ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                ],
            ),
            ModelResponse(
                reasoning_content=sentinel,
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Reviewed"},
                    )
                ],
            ),
            ModelResponse(
                reasoning_content=sentinel,
                tool_calls=[
                    ToolCall(
                        id="verify-pass",
                        name="submit_verification",
                        arguments={
                            "verdict": "pass",
                            "summary": "Verified",
                            "findings": [],
                        },
                    )
                ],
            ),
        ]
    )
    manager = AgentManager(
        replace(settings, model="gpt-5.6-sol", base_url=None), storage, provider
    )

    run = await manager.start_run(
        "Review the workspace", reasoning_effort=ReasoningEffort.HIGH
    )
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    assert completed.reasoning_effort is ReasoningEffort.HIGH
    assert completed.turns[-1].reasoning_effort is ReasoningEffort.HIGH
    assert provider.reasoning_efforts == [ReasoningEffort.HIGH] * 3
    requested = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.MODEL_REQUESTED
    ]
    assert [event.payload["requested_effort"] for event in requested] == [
        "high",
        "high",
        "high",
    ]
    assert all(event.payload["wire_effort"] == "high" for event in requested)
    assert sentinel not in json.dumps(completed.messages)
    assert sentinel not in json.dumps(
        [event.model_dump(mode="json") for event in storage.get_events(run.id)]
    )
    pack = build_proof_pack(completed, storage)
    assert pack.turns[-1].reasoning_effort is ReasoningEffort.HIGH
    assert sentinel not in pack.model_dump_json()
    assert sentinel not in proof_pack_markdown(pack)
    for database_file in settings.data_dir.glob("test.db*"):
        assert sentinel.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_terminal_transition_scrubs_private_reasoning_before_state_persist(
    settings: Settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "PRIVATE-TERMINAL-ORDER-SENTINEL"
    run = RunRecord(
        id="terminal-order",
        task="Finish safely",
        workspace=str(settings.workspace),
        state=RunState.VERIFYING,
        messages=[
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": sentinel,
            }
        ],
    )
    storage.create_run(run)
    manager = AgentManager(settings, storage, ScriptedProvider([]))

    def reject_checkpoint() -> None:
        persisted = storage.get_run(run.id)
        assert persisted.state is RunState.VERIFYING
        assert sentinel not in json.dumps(persisted.messages)
        assert persisted.provider_reasoning_cleanup_pending is True
        raise SecureCheckpointError("simulated busy WAL")

    monkeypatch.setattr(storage, "secure_checkpoint", reject_checkpoint)

    transitioned = await manager._transition(run, RunState.SUCCEEDED)

    assert transitioned is False
    assert run.state is RunState.INTERRUPTED
    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.INTERRUPTED
    assert persisted.interrupted_from is RunState.VERIFYING
    assert persisted.provider_reasoning_cleanup_pending is True


@pytest.mark.asyncio
async def test_cleanup_checkpoint_failure_interrupts_worker_without_a_ghost_run(
    settings: Settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "PRIVATE-RECOVERABLE-CLEANUP-SENTINEL"
    provider = ScriptedProvider(
        [
            ModelResponse(
                reasoning_content=sentinel,
                tool_calls=[
                    ToolCall(
                        id="answer",
                        name="respond_to_user",
                        arguments={"content": "Safe public answer"},
                    )
                ],
            )
        ]
    )
    manager = AgentManager(
        replace(
            settings,
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
        ),
        storage,
        provider,
    )
    original_checkpoint = storage.secure_checkpoint

    def reject_checkpoint() -> None:
        raise SecureCheckpointError("simulated persistent reader")

    monkeypatch.setattr(storage, "secure_checkpoint", reject_checkpoint)
    run = await manager.start_run(
        "Answer safely", reasoning_effort=ReasoningEffort.HIGH
    )
    interrupted = await manager.wait(run.id)

    assert interrupted.state is RunState.INTERRUPTED
    assert interrupted.provider_reasoning_cleanup_pending is True
    assert manager._tasks[run.id].done()
    assert storage.has_live_run() is False
    assert sentinel not in json.dumps(interrupted.messages)
    cleanup_error = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.ERROR
        and event.payload.get("cause") == "provider_reasoning_cleanup_pending"
    )
    assert cleanup_error.payload["recoverable"] is True

    with pytest.raises(InvalidRunAction, match="cleanup is still waiting"):
        await manager.resume(run.id)
    assert manager._tasks[run.id].done()

    monkeypatch.setattr(storage, "secure_checkpoint", original_checkpoint)
    cancelled = await manager.cancel(run.id)

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.provider_reasoning_cleanup_pending is False
    for database_file in settings.data_dir.glob("test.db*"):
        assert sentinel.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_credential_like_private_reasoning_fails_closed_without_persistence(
    settings: Settings, storage: Storage
) -> None:
    sentinel = "sk-abcdefghijklmnop"
    provider = ScriptedProvider(
        [
            ModelResponse(
                reasoning_content=sentinel,
                tool_calls=[
                    ToolCall(
                        id="answer",
                        name="respond_to_user",
                        arguments={"content": "Safe public answer"},
                    )
                ],
            )
        ]
    )
    manager = AgentManager(
        replace(
            settings,
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
        ),
        storage,
        provider,
    )

    run = await manager.start_run(
        "Answer safely", reasoning_effort=ReasoningEffort.HIGH
    )
    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert completed.error == "Provider-private replay state could not be stored safely"
    persisted = json.dumps(completed.model_dump(mode="json"), ensure_ascii=False)
    events = json.dumps(
        [event.model_dump(mode="json") for event in storage.get_events(run.id)],
        ensure_ascii=False,
    )
    assert sentinel not in persisted
    assert sentinel not in events
    for database_file in settings.data_dir.glob("test.db*"):
        assert sentinel.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_location",
    ["argument_value", "argument_key", "call_id", "call_name"],
)
async def test_provider_tool_credentials_fail_before_execution_or_persistence(
    settings: Settings,
    storage: Storage,
    secret_location: str,
) -> None:
    configured = 'owner"secret\\tail\tsegment'
    arguments = {"path": "missing.txt"}
    call_id = "unsafe-read"
    call_name = "read_file"
    if secret_location == "argument_value":
        arguments["path"] = configured
    elif secret_location == "argument_key":
        arguments[configured] = "provider-controlled"
    elif secret_location == "call_id":
        call_id = configured
    else:
        call_name = configured
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id=call_id, name=call_name, arguments=arguments)
                ]
            ),
            _direct_response("This fallback must not be requested."),
        ]
    )
    manager = AgentManager(replace(settings, api_key=configured), storage, provider)

    run = await manager.start_run("Inspect a safe path")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    persisted = json.dumps(
        {
            "run": completed.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
        },
        ensure_ascii=False,
    )

    assert completed.state is RunState.FAILED
    assert completed.error == (
        "Provider tool call contained credential-like data and was rejected before "
        "storage or execution"
    )
    assert len(provider.requests) == 1
    assert not any(
        event.type in {EventType.TOOL_REQUESTED, EventType.TOOL_STARTED, EventType.TOOL_COMPLETED}
        for event in events
    )
    assert configured not in persisted
    escaped = json.dumps(configured, ensure_ascii=False)[1:-1].encode()
    for database_file in settings.data_dir.glob("test.db*"):
        database_bytes = database_file.read_bytes()
        assert configured.encode() not in database_bytes
        assert escaped not in database_bytes


@pytest.mark.asyncio
async def test_structural_json_cannot_synthesize_a_credential_in_agent_history(
    settings: Settings, storage: Storage
) -> None:
    configured = 'foo", "start_line": 1'
    arguments = {"path": "foo", "start_line": 1}
    assert configured in json.dumps(arguments)
    assert not contains_redactable_json_secret(arguments, api_key=configured)
    (settings.workspace / "foo").write_text("safe evidence\n")
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="inspect", name="read_file", arguments=arguments)
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
                        arguments={"summary": "Reviewed safely"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(replace(settings, api_key=configured), storage, provider)

    run = await manager.start_run("Review foo", verifier_enabled=False)
    completed = await manager.wait(run.id)
    storage.secure_checkpoint()

    assert completed.state is RunState.SUCCEEDED
    assert any(
        event.type is EventType.TOOL_COMPLETED
        and event.payload.get("call", {}).get("id") == "inspect"
        for event in storage.get_events(run.id)
    )
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_tool_result_metadata_is_recursively_redacted_before_persistence(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = "owner-only-key-123456"
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="inspect",
                        name="read_file",
                        arguments={"path": "safe.txt"},
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
                        arguments={"summary": "Reviewed safely"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(replace(settings, api_key=configured), storage, provider)

    async def metadata_result(
        _run_id: str, call: ToolCall, **_kwargs: object
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            ok=True,
            output="safe evidence",
            metadata={configured: {"nested": configured}},
        )

    monkeypatch.setattr(manager.tools, "execute", metadata_result)
    run = await manager.start_run("Review safely", verifier_enabled=False)
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)

    assert completed.state is RunState.SUCCEEDED
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    assert configured not in serialized
    metadata = next(
        event.payload["result"]["metadata"]
        for event in events
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["id"] == "inspect"
    )
    assert metadata == {"█" * 10: {"nested": "█" * 10}}
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_secret_bearing_builder_call_cannot_mutate_or_open_approval(
    settings: Settings, storage: Storage
) -> None:
    configured = "owner-only-key-123456"
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
                        id="unsafe-write",
                        name="create_file",
                        arguments={"path": "unsafe.txt", "content": configured},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(replace(settings, api_key=configured), storage, provider)

    run = await manager.start_run("Review without writing credentials", verifier_enabled=False)
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    persisted = json.dumps(
        {
            "run": completed.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
        },
        ensure_ascii=False,
    )

    assert completed.state is RunState.FAILED
    assert completed.pending_approval is None
    assert not (settings.workspace / "unsafe.txt").exists()
    assert not any(
        event.type is EventType.APPROVAL_REQUESTED
        or event.payload.get("id") == "unsafe-write"
        or (
            isinstance(event.payload.get("call"), dict)
            and event.payload["call"].get("id") == "unsafe-write"
        )
        for event in events
    )
    assert configured not in persisted
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_secret_bearing_verifier_report_fails_before_publication(
    settings: Settings, storage: Storage
) -> None:
    configured = "owner-only-key-123456"
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
                        arguments={"summary": "Reviewed safely"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="unsafe-verdict",
                        name="submit_verification",
                        arguments={
                            "verdict": "pass",
                            "summary": configured,
                            "findings": [],
                        },
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(replace(settings, api_key=configured), storage, provider)

    run = await manager.start_run("Review safely")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)

    assert completed.state is RunState.FAILED
    assert completed.verification is None
    assert not any(
        event.type is EventType.VERIFICATION_COMPLETED for event in events
    )
    assert configured not in json.dumps(
        {
            "run": completed.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
        },
        ensure_ascii=False,
    )
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_terminal_presentation_credentials_are_redacted_not_executed(
    settings: Settings, storage: Storage
) -> None:
    configured = 'owner"secret\\tail\tsegment'
    protected_settings = replace(settings, api_key=configured)
    direct = AgentManager(
        protected_settings,
        storage,
        ScriptedProvider([_direct_response(f"Safe prefix {configured} suffix")]),
    )
    direct_run = await direct.start_run("Answer safely")
    answered = await direct.wait(direct_run.id)

    finish = AgentManager(
        protected_settings,
        storage,
        ScriptedProvider(
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
                            arguments={"summary": f"Reviewed {configured}"},
                        )
                    ]
                ),
            ]
        ),
    )
    finish_run = await finish.start_run("Review safely", verifier_enabled=False)
    completed = await finish.wait(finish_run.id)

    assert answered.state is RunState.ANSWERED
    assert completed.state is RunState.SUCCEEDED
    assert answered.turns[-1].summary == redact_text(
        f"Safe prefix {configured} suffix", api_key=configured
    )
    assert completed.turns[-1].summary == redact_text(
        f"Reviewed {configured}", api_key=configured
    )
    persisted = json.dumps(
        {
            "runs": [run.model_dump(mode="json") for run in storage.list_runs()],
            "events": {
                run.id: [
                    event.model_dump(mode="json") for event in storage.get_events(run.id)
                ]
                for run in storage.list_runs()
            },
        },
        ensure_ascii=False,
    )
    assert configured not in persisted
    escaped = json.dumps(configured, ensure_ascii=False)[1:-1].encode()
    for database_file in settings.data_dir.glob("test.db*"):
        database_bytes = database_file.read_bytes()
        assert configured.encode() not in database_bytes
        assert escaped not in database_bytes


@pytest.mark.asyncio
async def test_user_task_credentials_are_rejected_before_run_creation(
    settings: Settings, storage: Storage
) -> None:
    configured = "owner-only-key-123456"
    manager = AgentManager(
        replace(settings, api_key=configured),
        storage,
        ScriptedProvider([]),
    )

    with pytest.raises(
        ValueError,
        match="Task text contains credential-like data; remove it before starting",
    ):
        await manager.start_run(f"Please inspect {configured}")

    assert storage.list_runs() == []
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_follow_up_credentials_are_rejected_without_reopening_the_turn(
    settings: Settings, storage: Storage
) -> None:
    configured = "owner-only-key-123456"
    provider = ScriptedProvider([_direct_response("Safe answer")])
    manager = AgentManager(replace(settings, api_key=configured), storage, provider)
    run = await manager.start_run("Answer safely")
    answered = await manager.wait(run.id)

    with pytest.raises(
        ValueError,
        match="Follow-up text contains credential-like data; remove it before continuing",
    ):
        await manager.follow_up(run.id, f"Continue with {configured}")

    unchanged = storage.get_run(run.id)
    assert unchanged.state is RunState.ANSWERED
    assert unchanged.turns == answered.turns
    assert len(provider.requests) == 1
    assert configured not in json.dumps(
        {
            "run": unchanged.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json") for event in storage.get_events(run.id)
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_human_decision_credentials_are_rejected_before_durable_acceptance(
    settings: Settings, storage: Storage
) -> None:
    configured = "owner-only-key-123456"
    protected_settings = replace(settings, api_key=configured)

    clarification_manager = AgentManager(
        protected_settings,
        storage,
        ScriptedProvider([_question_response("question")]),
    )
    clarification_run = await clarification_manager.start_run("Clarify safely")
    await _wait_for_state(
        storage, clarification_run.id, RunState.AWAITING_CLARIFICATION
    )
    clarification_receipt = storage.get_active_decision(clarification_run.id)
    assert clarification_receipt is not None
    with pytest.raises(
        ValueError,
        match="Clarification answer contains credential-like data",
    ):
        await clarification_manager.answer_clarification(
            clarification_run.id,
            [ClarificationAnswer(question_id="choice", custom_text=configured)],
        )
    assert storage.get_decision(
        clarification_run.id, clarification_receipt.request_id
    ).status is DecisionStatus.PENDING
    await clarification_manager.cancel(clarification_run.id)

    plan_manager = AgentManager(
        protected_settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                    ]
                )
            ]
        ),
    )
    plan_run = await plan_manager.start_run("Plan safely", mode=InteractionMode.PLAN)
    await _wait_for_state(storage, plan_run.id, RunState.AWAITING_PLAN_APPROVAL)
    plan_receipt = storage.get_active_decision(plan_run.id)
    assert plan_receipt is not None
    with pytest.raises(
        ValueError,
        match="Plan decision contains credential-like data",
    ):
        await plan_manager.decide_plan(
            plan_run.id,
            PlanDecision(decision="revise", feedback=configured),
        )
    assert storage.get_decision(
        plan_run.id, plan_receipt.request_id
    ).status is DecisionStatus.PENDING
    await plan_manager.cancel(plan_run.id)

    persisted = json.dumps(
        {
            "runs": [run.model_dump(mode="json") for run in storage.list_runs()],
            "events": {
                run.id: [
                    event.model_dump(mode="json") for event in storage.get_events(run.id)
                ]
                for run in storage.list_runs()
            },
        },
        ensure_ascii=False,
    )
    assert configured not in persisted
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


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
                        arguments={"summary": "Reviewed"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Review context.txt", verifier_enabled=False, mode=InteractionMode.PLAN
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    builder_messages = provider.requests[2][0]
    builder_context = str(builder_messages[1]["content"])
    arguments_start = builder_context.index("### read_file(") + len("### read_file(")
    arguments_end = builder_context.index(")\n", arguments_start)
    assert json.loads(builder_context[arguments_start:arguments_end]) == {
        "path": "context.txt"
    }
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
                        arguments={"summary": "Reviewed"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Review", verifier_enabled=False, mode=InteractionMode.PLAN
    )
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
async def test_stale_plan_retry_is_idempotent_without_rebinding_to_revised_plan(
    settings: Settings, storage: Storage
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="submit_plan",
                        arguments=_review_plan("Draft"),
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-2",
                        name="submit_plan",
                        arguments=_review_plan("Revised"),
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Reviewed the revised plan"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Review safely", verifier_enabled=False, mode=InteractionMode.PLAN
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    first = storage.get_active_decision(run.id)
    assert first is not None
    revision = PlanDecision(decision="revise", feedback="Make it narrower")

    await manager.decide_plan(run.id, revision, request_id=first.request_id)
    async with asyncio.timeout(3):
        while True:
            waiting = storage.get_run(run.id)
            second = storage.get_active_decision(run.id)
            if (
                waiting.state is RunState.AWAITING_PLAN_APPROVAL
                and waiting.plan is not None
                and waiting.plan.summary == "Revised"
                and second is not None
                and second.request_id != first.request_id
            ):
                break
            await asyncio.sleep(0.01)

    await manager.decide_plan(run.id, revision, request_id=first.request_id)
    still_waiting = storage.get_run(run.id)
    assert still_waiting.state is RunState.AWAITING_PLAN_APPROVAL
    assert still_waiting.plan is not None and still_waiting.plan.summary == "Revised"
    assert storage.get_active_decision(run.id) == second
    with pytest.raises(InvalidRunAction, match="different response"):
        await manager.decide_plan(
            run.id,
            PlanDecision(decision="approve"),
            request_id=first.request_id,
        )

    await manager.decide_plan(
        run.id, PlanDecision(decision="approve"), request_id=second.request_id
    )
    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED


@pytest.mark.asyncio
async def test_stale_clarification_retry_does_not_answer_the_next_round(
    settings: Settings, storage: Storage
) -> None:
    def question(call_id: str) -> ModelResponse:
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="ask_questions",
                    arguments={
                        "questions": [
                            {
                                "id": "choice",
                                "prompt": "Choose one",
                                "options": [
                                    {"id": "a", "label": "A"},
                                    {"id": "b", "label": "B"},
                                ],
                            }
                        ]
                    },
                )
            ]
        )

    manager = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [question("question-1"), question("question-2"), _direct_response("Done")]
        ),
    )
    run = await manager.start_run("Clarify twice")
    await _wait_for_state(storage, run.id, RunState.AWAITING_CLARIFICATION)
    first = storage.get_active_decision(run.id)
    assert first is not None
    first_answers = [ClarificationAnswer(question_id="choice", option_id="a")]
    await manager.answer_clarification(
        run.id, first_answers, request_id=first.request_id
    )
    async with asyncio.timeout(3):
        while True:
            waiting = storage.get_run(run.id)
            second = storage.get_active_decision(run.id)
            if (
                waiting.state is RunState.AWAITING_CLARIFICATION
                and waiting.clarification is not None
                and waiting.clarification.round == 2
                and second is not None
                and second.request_id != first.request_id
            ):
                break
            await asyncio.sleep(0.01)

    await manager.answer_clarification(
        run.id, first_answers, request_id=first.request_id
    )
    assert storage.get_active_decision(run.id) == second
    assert storage.get_run(run.id).clarification is not None
    with pytest.raises(InvalidRunAction, match="different response"):
        await manager.answer_clarification(
            run.id,
            [ClarificationAnswer(question_id="choice", option_id="b")],
            request_id=first.request_id,
        )
    await manager.answer_clarification(
        run.id,
        [ClarificationAnswer(question_id="choice", option_id="b")],
        request_id=second.request_id,
    )
    completed = await manager.wait(run.id)
    assert completed.state is RunState.ANSWERED


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
                        arguments={"summary": "Draft"},
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
                        arguments={"summary": "Final"},
                    )
                ]
            ),
            _verification("pass", "The repaired value satisfies the plan."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Create a final value", mode=InteractionMode.PLAN)
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
                        arguments={"summary": "Draft"},
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
                        arguments={"summary": "Final"},
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
                        arguments={"summary": "Final"},
                    )
                ]
            ),
            _verification("pass", "The repaired value and fresh check satisfy the plan."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Create a final value with current evidence", mode=InteractionMode.PLAN
    )
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
                        arguments={"summary": "Done"},
                    )
                ]
            ),
            _verification("inconclusive", "Evidence is insufficient."),
        ]
    )
    manager = AgentManager(limited, storage, provider)
    run = await manager.start_run("Prove the result", mode=InteractionMode.PLAN)
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
    run = await manager.start_run(
        "Try a missing tool", verifier_enabled=False, mode=InteractionMode.PLAN
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.FAILED
    assert "failed three times" in (completed.error or "")


@pytest.mark.asyncio
async def test_recovery_guidance_follows_every_result_in_a_parallel_tool_batch(
    settings: Settings, storage: Storage
) -> None:
    repeated_arguments = {"path": "missing.txt"}
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
                        id="first-failure",
                        name="read_file",
                        arguments=repeated_arguments,
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="second-failure",
                        name="read_file",
                        arguments=repeated_arguments,
                    ),
                    ToolCall(
                        id="list-after-failure",
                        name="list_files",
                        arguments={"path": "."},
                    ),
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "已检查工作区并完成恢复。"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("检查空工作区", verifier_enabled=False)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    finish_request = provider.requests[-1][0]
    second_batch = next(
        index
        for index, message in enumerate(finish_request)
        if any(
            call.get("id") == "second-failure"
            for call in message.get("tool_calls", [])
        )
    )
    tail = finish_request[second_batch + 1 :]
    assert [message.get("tool_call_id") for message in tail[:2]] == [
        "second-failure",
        "list-after-failure",
    ]
    assert tail[2]["role"] == "system"
    assert "recovery mode" in tail[2]["content"]


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
                        arguments={"summary": "Ran"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Run Python", verifier_enabled=False, mode=InteractionMode.PLAN
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    approval = storage.get_run(run.id).pending_approval
    assert approval is not None
    await manager.decide_action(run.id, approval.id, approved=True)

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    results = [
        event.payload["result"]
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["name"] == "run_command"
    ]
    assert [result["output"] for result in results] == ["approved\n"]
    assert results[0]["metadata"]["sandbox"]["status"] == "bypassed"


@pytest.mark.asyncio
async def test_manual_mode_asks_for_planned_edit_and_check_without_sandbox_bypass(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ["uv", "run", "pytest", "-q"]
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="submit_plan",
                        arguments={
                            **_plan_arguments(command),
                            "impacted_files": ["manual.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="create_file",
                        arguments={"path": "manual.txt", "content": "approved\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="check",
                        name="run_command",
                        arguments={"argv": command},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Approved"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    original_execute = manager.tools.execute
    command_bypasses: list[bool] = []

    async def execute(
        run_id: str,
        call: ToolCall,
        *,
        output_callback=None,
        sandbox_bypass: bool = False,
    ) -> ToolResult:
        if call.name != "run_command":
            return await original_execute(
                run_id,
                call,
                output_callback=output_callback,
                sandbox_bypass=sandbox_bypass,
            )
        command_bypasses.append(sandbox_bypass)
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            ok=True,
            output="1 passed\n",
            metadata={
                "exit_code": 0,
                "sandbox": {
                    "status": "enforced",
                    "backend": "seatbelt",
                    "enforced": True,
                },
            },
        )

    monkeypatch.setattr(manager.tools, "execute", execute)
    run = await manager.start_run(
        "Create manual.txt",
        verifier_enabled=False,
        approval_mode=ApprovalMode.MANUAL,
    )

    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    write_approval = storage.get_run(run.id).pending_approval
    assert write_approval is not None
    assert write_approval.tool_call.id == "write"
    assert write_approval.policy_decision == "allow"
    assert write_approval.sandbox_bypass_on_approve is False
    await manager.decide_action(run.id, write_approval.id, approved=True)

    async with asyncio.timeout(3):
        while True:
            next_approval = storage.get_run(run.id).pending_approval
            if next_approval is not None and next_approval.id != write_approval.id:
                break
            await asyncio.sleep(0.01)
    check_approval = storage.get_run(run.id).pending_approval
    assert check_approval is not None
    assert check_approval.tool_call.id == "check"
    assert check_approval.policy_decision == "allow"
    assert check_approval.sandbox_bypass_on_approve is False
    await manager.decide_action(run.id, check_approval.id, approved=True)

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    assert (settings.workspace / "manual.txt").read_text() == "approved\n"
    assert command_bypasses == [False]
    assert completed.approval_mode is ApprovalMode.MANUAL
    assert completed.turns[-1].approval_mode is ApprovalMode.MANUAL


@pytest.mark.asyncio
async def test_full_access_auto_allows_scope_drift_but_not_hard_denials(
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
                            "summary": "Create a file",
                            "steps": [{"id": "create", "title": "Create it"}],
                            "acceptance_checks": [
                                {"id": "review", "label": "The file exists"}
                            ],
                            "impacted_files": ["planned.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="drift",
                        name="create_file",
                        arguments={"path": "actual.txt", "content": "inside\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Created"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Create a file",
        verifier_enabled=False,
        approval_mode=ApprovalMode.FULL_ACCESS,
    )

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    assert (settings.workspace / "actual.txt").read_text() == "inside\n"
    assert not any(
        event.type is EventType.APPROVAL_REQUESTED
        for event in storage.get_events(run.id)
    )
    result = next(
        event.payload["result"]
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["id"] == "drift"
    )
    assert result["metadata"]["permission"]["mode"] == "full_access"
    assert result["metadata"]["permission"]["policy_decision"] == "ask"
    assert result["metadata"]["permission"]["sandbox_bypass"] is False


@pytest.mark.asyncio
async def test_full_access_unknown_command_auto_runs_only_inside_enforced_sandbox(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
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
                        id="unknown",
                        name="run_command",
                        arguments={"argv": ["python", "app.py"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Ran safely"},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    manager.tools.sandbox.status = SandboxStatus(
        backend="seatbelt", enforced=True, detail="test sandbox"
    )
    bypasses: list[bool] = []

    async def execute(
        _run_id: str,
        call: ToolCall,
        *,
        output_callback=None,
        sandbox_bypass: bool = False,
    ) -> ToolResult:
        del output_callback
        bypasses.append(sandbox_bypass)
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            ok=True,
            output="safe\n",
            metadata={
                "exit_code": 0,
                "sandbox": {
                    "status": "enforced",
                    "backend": "seatbelt",
                    "enforced": True,
                },
            },
        )

    monkeypatch.setattr(manager.tools, "execute", execute)
    run = await manager.start_run(
        "Run unknown code",
        verifier_enabled=False,
        approval_mode=ApprovalMode.FULL_ACCESS,
    )

    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    assert bypasses == [False]
    assert not any(
        event.type is EventType.APPROVAL_REQUESTED
        for event in storage.get_events(run.id)
    )
    command_result = next(
        event.payload["result"]
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["id"] == "unknown"
    )
    assert command_result["metadata"]["sandbox"]["status"] == "enforced"
    assert command_result["metadata"]["permission"]["sandbox_bypass"] is False


@pytest.mark.asyncio
async def test_cancel_abandons_pending_approval_and_stale_id_cannot_resolve_it(
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
                        id="unknown",
                        name="run_command",
                        arguments={"argv": ["python", "app.py"]},
                    )
                ]
            ),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run(
        "Run a command",
        verifier_enabled=False,
        approval_mode=ApprovalMode.AUTOMATIC,
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    approval = storage.get_run(run.id).pending_approval
    assert approval is not None

    with pytest.raises(InvalidRunAction, match="no longer pending"):
        await manager.decide_action(run.id, "stale-id", approved=True)
    await manager.cancel(run.id)

    cancelled = storage.get_run(run.id)
    assert cancelled.state is RunState.CANCELLED
    assert cancelled.pending_approval is None
    resolved = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.APPROVAL_RESOLVED
    ]
    assert resolved[-1].payload["approval_id"] == approval.id
    assert resolved[-1].payload["outcome"] == "abandoned"
    assert resolved[-1].payload["cause"] == "user_cancelled"


@pytest.mark.asyncio
async def test_restart_reopens_pending_approval_without_replaying_action(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                    ]
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="pending-command",
                            name="run_command",
                            arguments={"argv": ["python", "app.py"]},
                        )
                    ]
                ),
            ]
        ),
    )
    run = await first.start_run("Run later", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    approval = storage.get_run(run.id).pending_approval
    assert approval is not None

    await first.shutdown()

    interrupted = storage.get_run(run.id)
    assert interrupted.state is RunState.INTERRUPTED
    assert interrupted.pending_approval is not None
    assert interrupted.pending_approval.id == approval.id
    assert interrupted.interrupted_from is RunState.AWAITING_ACTION_APPROVAL
    receipt = storage.get_active_decision(run.id)
    assert receipt is not None
    assert receipt.request_id == approval.id

    second = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish-after-resume",
                            name="finish",
                            arguments={
                                "summary": "Inspected after restart",
                            },
                        )
                    ]
                )
            ]
        ),
    )
    await second.resume(run.id)
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    reopened = storage.get_run(run.id).pending_approval
    assert reopened is not None and reopened.id == approval.id
    await second.decide_action(run.id, approval.id, approved=False)
    completed = await second.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    events = storage.get_events(run.id)
    pending_results = [
        event
        for event in events
        if event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["id"] == "pending-command"
    ]
    assert len(pending_results) == 1
    assert pending_results[0].payload["result"]["ok"] is False
    assert not any(
        event.type is EventType.TOOL_STARTED
        and event.payload.get("id") == "pending-command"
        for event in events
    )


@pytest.mark.asyncio
async def test_restart_refuses_legacy_secret_bearing_approval_without_execution(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = "owner-only-key-123456"
    protected_settings = replace(settings, api_key=configured)
    first = AgentManager(
        protected_settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                    ]
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="pending-command",
                            name="run_command",
                            arguments={"argv": ["python", "app.py"]},
                        )
                    ]
                ),
            ]
        ),
    )
    run = await first.start_run("Run later", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    await first.shutdown()

    legacy = storage.get_run(run.id)
    assert legacy.pending_approval is not None
    approval_id = legacy.pending_approval.id
    legacy.pending_approval.tool_call.arguments = {
        "argv": ["python", "-c", configured]
    }
    with sqlite3.connect(settings.data_dir / "test.db") as legacy_writer:
        legacy_writer.execute(
            "UPDATE runs SET pending_approval_json = ? WHERE id = ?",
            (legacy.pending_approval.model_dump_json(), run.id),
        )
    baseline_seq = storage.get_events(run.id)[-1].seq

    second = AgentManager(
        protected_settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="safe-finish",
                            name="finish",
                            arguments={"summary": "Recovered without replay"},
                        )
                    ]
                )
            ]
        ),
    )
    executed: list[ToolCall] = []

    async def execute(_run_id: str, call: ToolCall, **_kwargs: object) -> ToolResult:
        executed.append(call)
        raise AssertionError("Unsafe persisted approval must never execute")

    monkeypatch.setattr(second.tools, "execute", execute)
    with pytest.raises(InvalidRunAction, match="persisted tool call is unsafe"):
        await second.decide_action(run.id, approval_id, approved=True)

    await second.resume(run.id)
    completed = await second.wait(run.id)
    new_events = [
        event for event in storage.get_events(run.id) if event.seq > baseline_seq
    ]

    assert completed.state is RunState.SUCCEEDED
    assert completed.pending_approval is None
    assert completed.error is None
    assert storage.get_decision(run.id, approval_id).status is DecisionStatus.ABANDONED
    assert executed == []
    assert configured not in json.dumps(
        [event.model_dump(mode="json") for event in new_events], ensure_ascii=False
    )
    assert not any(
        event.type is EventType.APPROVAL_REQUESTED for event in new_events
    )


@pytest.mark.asyncio
async def test_unsafe_accepted_legacy_action_requires_destructive_cancel_without_execution(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = "owner-only-key-123456"
    protected_settings = replace(settings, api_key=configured)
    first = AgentManager(
        protected_settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                    ]
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="pending-command",
                            name="run_command",
                            arguments={"argv": ["python", "app.py"]},
                        )
                    ]
                ),
            ]
        ),
    )
    run = await first.start_run("Run later", verifier_enabled=False)
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    waiting = storage.get_run(run.id)
    assert waiting.pending_approval is not None
    approval_id = waiting.pending_approval.id
    storage.accept_decision(
        run.id,
        approval_id,
        DecisionKind.ACTION,
        {"approved": True},
    )
    await first.shutdown()

    legacy = storage.get_run(run.id)
    assert legacy.pending_approval is not None
    legacy.pending_approval.tool_call.arguments = {
        "argv": ["python", "-c", configured]
    }
    with sqlite3.connect(settings.data_dir / "test.db") as legacy_writer:
        legacy_writer.execute(
            "UPDATE runs SET pending_approval_json = ? WHERE id = ?",
            (legacy.pending_approval.model_dump_json(), run.id),
        )
    baseline_seq = storage.get_events(run.id)[-1].seq

    second = AgentManager(protected_settings, storage, ScriptedProvider([]))
    executed: list[ToolCall] = []

    async def execute(_run_id: str, call: ToolCall, **_kwargs: object) -> ToolResult:
        executed.append(call)
        raise AssertionError("Unsafe accepted approval must never execute")

    monkeypatch.setattr(second.tools, "execute", execute)
    with pytest.raises(InvalidRunAction, match="stored context conflicts"):
        await second.resume(run.id)
    completed = await second.cancel(run.id)
    new_events = [
        event for event in storage.get_events(run.id) if event.seq > baseline_seq
    ]

    assert completed.state is RunState.CANCELLED
    assert completed.pending_approval is None
    assert completed.error is None
    assert completed.messages == []
    assert completed.turns[-1].outcome == "cancelled"
    assert storage.get_decision(run.id, approval_id).status is DecisionStatus.ABANDONED
    assert executed == []
    assert configured not in json.dumps(
        [event.model_dump(mode="json") for event in new_events], ensure_ascii=False
    )
    assert not any(
        event.type in {EventType.APPROVAL_REQUESTED, EventType.TOOL_STARTED}
        for event in new_events
    )


@pytest.mark.asyncio
async def test_rotated_credential_context_can_be_cancelled_without_model_or_tool_side_effects(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotated_credential = "rotated-owner-credential-987654"
    run = RunRecord(
        id="rotated-context-cancel",
        task="Inspect a saved response",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        turns=[ConversationTurn(index=1, request=rotated_credential)],
        messages=[{"role": "assistant", "content": rotated_credential}],
    )
    snapshot = WorkspaceInstructionSnapshot.empty()
    storage.create_run(run, instruction_snapshot=snapshot)
    storage.append_event(
        run.id,
        EventType.ASSISTANT_OUTPUT_STARTED,
        {"stream_id": "credential-conflict-stream", "status": "streaming"},
    )
    baseline_seq = storage.get_events(run.id)[-1].seq if storage.get_events(run.id) else 0
    provider = ScriptedProvider([])
    manager = AgentManager(
        replace(settings, api_key=rotated_credential),
        storage,
        provider,
    )

    async def forbidden_tool_side_effect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Credential-conflict recovery must not invoke tools")

    monkeypatch.setattr(manager.tools, "cancel", forbidden_tool_side_effect)
    monkeypatch.setattr(manager.tools, "execute", forbidden_tool_side_effect)

    with pytest.raises(InvalidRunAction, match="stored context conflicts"):
        await manager.resume(run.id)
    cancelled = await manager.cancel(run.id)
    new_events = [
        event for event in storage.get_events(run.id) if event.seq > baseline_seq
    ]

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.interrupted_from is None
    assert cancelled.messages == []
    assert cancelled.plan is None
    assert cancelled.clarification is None
    assert cancelled.pending_approval is None
    assert cancelled.verification is None
    assert cancelled.plan_gate is None
    assert cancelled.error is None
    assert cancelled.provider_reasoning_cleanup_pending is False
    assert cancelled.turns[-1].outcome == "cancelled"
    assert rotated_credential not in cancelled.model_dump_json()
    assert provider.requests == []
    assert storage.has_active_run(settings.workspace) is False
    assert (
        storage.get_workspace_instruction_snapshot(run.id, 1).snapshot_sha256
        == snapshot.snapshot_sha256
    )
    assert [event.type for event in new_events] == [
        EventType.ASSISTANT_OUTPUT_ABORTED,
        EventType.STATE_CHANGED,
        EventType.TURN_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert new_events[0].payload == {
        "status": "cancelled",
        "reason": "credential_conflict_cancelled",
        "all_open": True,
    }
    assert storage.abort_open_assistant_output_streams(
        run.id,
        status="cancelled",
        reason="test_probe",
    ) == []
    assert rotated_credential not in json.dumps(
        [event.model_dump(mode="json") for event in new_events], ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_credential_conflict_cancel_chooses_a_safe_timestamp(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferred_time = datetime(2026, 8, 28, 12, 34, 56, 123456, tzinfo=UTC)
    timestamp_credential = preferred_time.isoformat()[:12]
    run = RunRecord(
        id="timestamp-credential-conflict",
        task="Recover a timestamp collision",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        turns=[ConversationTurn(index=1, request=timestamp_credential)],
        messages=[{"role": "user", "content": timestamp_credential}],
    )
    storage.create_run(
        run,
        instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
    )
    monkeypatch.setattr("traceforge.storage.utc_now", lambda: preferred_time)
    manager = AgentManager(
        replace(settings, api_key=timestamp_credential),
        storage,
        ScriptedProvider([]),
    )

    with pytest.raises(InvalidRunAction, match="stored context conflicts"):
        await manager.resume(run.id)
    cancelled = await manager.cancel(run.id)
    events = storage.get_events(run.id)

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.created_at != preferred_time
    assert cancelled.updated_at == cancelled.created_at
    assert cancelled.turns[-1].started_at == cancelled.created_at
    assert cancelled.turns[-1].completed_at == cancelled.created_at
    assert all(event.created_at == cancelled.created_at for event in events)
    assert timestamp_credential not in cancelled.model_dump_json()
    assert timestamp_credential not in json.dumps(
        [event.model_dump(mode="json") for event in events],
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_credential_conflict_cancel_does_not_copy_an_unsafe_stream_id(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_id_credential = "rotated-stream-id-credential-987654"
    run = RunRecord(
        id="stream-id-credential-conflict",
        task="Recover an old stream identifier",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        turns=[ConversationTurn(index=1, request="Recover an old stream identifier")],
        messages=[{"role": "user", "content": "Resume safely"}],
    )
    storage.create_run(
        run,
        instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
    )
    storage.append_event(
        run.id,
        EventType.ASSISTANT_OUTPUT_STARTED,
        {"stream_id": stream_id_credential, "status": "streaming"},
    )
    baseline_seq = storage.get_events(run.id)[-1].seq
    provider = ScriptedProvider([])
    manager = AgentManager(
        replace(settings, api_key=stream_id_credential),
        storage,
        provider,
    )

    async def forbidden_tool_side_effect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Credential-conflict recovery must not invoke tools")

    monkeypatch.setattr(manager.tools, "cancel", forbidden_tool_side_effect)
    monkeypatch.setattr(manager.tools, "execute", forbidden_tool_side_effect)

    with pytest.raises(InvalidRunAction, match="stored context conflicts"):
        await manager.resume(run.id)
    cancelled = await manager.cancel(run.id)
    new_events = [
        event for event in storage.get_events(run.id) if event.seq > baseline_seq
    ]

    assert cancelled.state is RunState.CANCELLED
    assert provider.requests == []
    assert [event.type for event in new_events] == [
        EventType.ASSISTANT_OUTPUT_ABORTED,
        EventType.STATE_CHANGED,
        EventType.TURN_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert new_events[0].payload == {
        "status": "cancelled",
        "reason": "credential_conflict_cancelled",
        "all_open": True,
    }
    assert stream_id_credential not in json.dumps(
        [event.model_dump(mode="json") for event in new_events],
        ensure_ascii=False,
    )
    assert storage.abort_open_assistant_output_streams(
        run.id,
        status="cancelled",
        reason="test_probe",
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("current_index", [2, 3])
async def test_multiturn_credential_conflict_cancel_preserves_snapshot_indexes_for_follow_up(
    settings: Settings,
    storage: Storage,
    current_index: int,
) -> None:
    rotated_credential = f"rotated-turn-{current_index}-credential-987654"
    previous_turns = [
        ConversationTurn(
            index=index,
            request=f"Completed request {index}",
            outcome="answered",
            summary=f"Completed response {index}",
        )
        for index in range(1, current_index)
    ]
    run = RunRecord(
        id=f"multiturn-credential-conflict-{current_index}",
        task="Recover a multi-turn task",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        turns=[
            *previous_turns,
            ConversationTurn(index=current_index, request=rotated_credential),
        ],
        messages=[{"role": "user", "content": rotated_credential}],
    )
    storage.create_run(run)
    snapshots = [WorkspaceInstructionSnapshot.empty() for _ in range(current_index)]
    for turn_index, snapshot in enumerate(snapshots, start=1):
        storage.insert_workspace_instruction_snapshot(run.id, turn_index, snapshot)
    provider = ScriptedProvider([_direct_response("Recovered after cleanup")])
    manager = AgentManager(
        replace(settings, api_key=rotated_credential),
        storage,
        provider,
    )

    with pytest.raises(InvalidRunAction, match="stored context conflicts"):
        await manager.resume(run.id)
    cancelled = await manager.cancel(run.id)

    assert [turn.index for turn in cancelled.turns] == [current_index]
    for turn_index, snapshot in enumerate(snapshots, start=1):
        assert storage.get_workspace_instruction_snapshot(run.id, turn_index) == snapshot

    continued = await manager.follow_up(run.id, "Continue safely")
    completed = await manager.wait(continued.id)

    assert completed.state is RunState.ANSWERED
    assert [turn.index for turn in completed.turns] == [current_index, current_index + 1]
    assert completed.turns[-1].summary == "Recovered after cleanup"
    assert storage.get_workspace_instruction_snapshot(run.id, current_index + 1)
    for turn_index, snapshot in enumerate(snapshots, start=1):
        assert storage.get_workspace_instruction_snapshot(run.id, turn_index) == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "waiting_state"),
    [
        (DecisionKind.CLARIFICATION, RunState.AWAITING_CLARIFICATION),
        (DecisionKind.PLAN, RunState.AWAITING_PLAN_APPROVAL),
        (DecisionKind.ACTION, RunState.AWAITING_ACTION_APPROVAL),
    ],
)
@pytest.mark.parametrize("accepted", [False, True], ids=["pending", "accepted"])
async def test_rotated_credential_cancellation_abandons_every_active_decision_kind(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    kind: DecisionKind,
    waiting_state: RunState,
    accepted: bool,
) -> None:
    rotated_credential = f"rotated-{kind.value}-{accepted}-credential-987654"
    conflict = rotated_credential if not accepted else "safe subject"
    clarification = ClarificationRequest(
        questions=[
            ClarificationQuestion(
                id="scope",
                prompt=conflict,
                options=[
                    QuestionOption(id="safe", label="Safe"),
                    QuestionOption(id="alternate", label="Alternate"),
                ],
            )
        ]
    )
    plan = TaskPlan.model_validate(
        {
            **_review_plan(),
            "summary": conflict,
        }
    )
    approval = ApprovalRequest(
        id=f"rotated-{kind.value}-approval",
        tool_call=ToolCall(
            id=f"rotated-{kind.value}-call",
            name="run_command",
            arguments={"argv": ["python", "app.py"]},
        ),
        summary=conflict,
        reason="Manual approval is required",
        risk="elevated",
        approval_mode=ApprovalMode.MANUAL,
        policy_decision="ask",
    )
    previous_state = (
        RunState.EXECUTING if kind is DecisionKind.ACTION else RunState.PLANNING
    )
    request_id = approval.id if kind is DecisionKind.ACTION else f"{kind.value}-request"
    subject_model = {
        DecisionKind.CLARIFICATION: clarification,
        DecisionKind.PLAN: plan,
        DecisionKind.ACTION: approval,
    }[kind]
    run = RunRecord(
        id=f"rotated-{kind.value}-{accepted}",
        task="Recover a durable decision",
        workspace=str(settings.workspace),
        state=previous_state,
        turns=[ConversationTurn(index=1, request="Recover a durable decision")],
        clarification=clarification if kind is DecisionKind.CLARIFICATION else None,
        plan=plan if kind is DecisionKind.PLAN else None,
        pending_approval=approval if kind is DecisionKind.ACTION else None,
    )
    snapshot = WorkspaceInstructionSnapshot.empty()
    storage.create_run(run, instruction_snapshot=snapshot)
    run.state = waiting_state
    storage.open_decision(
        run,
        previous_state=previous_state,
        request_id=request_id,
        kind=kind,
        turn_index=1,
        subject=subject_model.model_dump(mode="json"),
        requested_event_type={
            DecisionKind.CLARIFICATION: EventType.CLARIFICATION_REQUESTED,
            DecisionKind.PLAN: EventType.PLAN_UPDATED,
            DecisionKind.ACTION: EventType.APPROVAL_REQUESTED,
        }[kind],
        requested_payload=subject_model.model_dump(mode="json"),
    )
    if accepted:
        storage.accept_decision(
            run.id,
            request_id,
            kind,
            {"accepted": True, "persisted_reply": rotated_credential},
        )
    interrupted = storage.get_run(run.id)
    interrupted.state = RunState.INTERRUPTED
    interrupted.interrupted_from = waiting_state
    storage.save_run(interrupted)
    baseline_seq = storage.get_events(run.id)[-1].seq
    provider = ScriptedProvider([])
    manager = AgentManager(
        replace(settings, api_key=rotated_credential),
        storage,
        provider,
    )

    async def forbidden_tool_side_effect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Credential-conflict recovery must not invoke tools")

    monkeypatch.setattr(manager.tools, "cancel", forbidden_tool_side_effect)
    monkeypatch.setattr(manager.tools, "execute", forbidden_tool_side_effect)

    with pytest.raises(InvalidRunAction, match="stored context conflicts"):
        await manager.resume(run.id)
    cancelled = await manager.cancel(run.id)
    receipt = storage.get_decision(run.id, request_id)
    new_events = [
        event for event in storage.get_events(run.id) if event.seq > baseline_seq
    ]

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.messages == []
    assert cancelled.plan is None
    assert cancelled.clarification is None
    assert cancelled.pending_approval is None
    assert cancelled.verification is None
    assert cancelled.plan_gate is None
    assert cancelled.turns[-1].outcome == "cancelled"
    assert storage.get_active_decision(run.id) is None
    assert receipt.status is DecisionStatus.ABANDONED
    assert receipt.payload is None
    assert receipt.payload_sha256 is None
    assert provider.requests == []
    assert storage.has_active_run(settings.workspace) is False
    assert (
        storage.get_workspace_instruction_snapshot(run.id, 1).snapshot_sha256
        == snapshot.snapshot_sha256
    )
    abandoned = [
        event for event in new_events if event.type is EventType.DECISION_ABANDONED
    ]
    assert len(abandoned) == 1
    assert abandoned[0].payload == {
        "kind": kind.value,
        "cause": "credential_conflict_cancelled",
        "unsafe_subject_discarded": True,
    }
    assert rotated_credential not in json.dumps(
        [event.model_dump(mode="json") for event in new_events], ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_resume_rejects_compact_json_credential_synthesized_from_safe_leaves(
    settings: Settings,
    storage: Storage,
) -> None:
    compact_credential = '"alpha":"omega"'
    run = RunRecord(
        id="compact-persisted-context",
        task="Inspect compact JSON boundaries",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        turns=[ConversationTurn(index=1, request="Inspect compact JSON boundaries")],
        messages=[{"alpha": "omega"}],
    )
    storage.create_run(
        run,
        instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
    )
    provider = ScriptedProvider([])
    manager = AgentManager(
        replace(settings, api_key=compact_credential),
        storage,
        provider,
    )

    with pytest.raises(InvalidRunAction, match="stored context conflicts"):
        await manager.resume(run.id)
    cancelled = await manager.cancel(run.id)

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.messages == []
    assert compact_credential not in cancelled.model_dump_json()
    assert provider.requests == []


@pytest.mark.asyncio
async def test_credential_conflict_cancel_releases_workspace_when_checkpoint_is_busy(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotated_credential = "rotated-busy-checkpoint-credential-987654"
    run = RunRecord(
        id="rotated-busy-checkpoint",
        task="Stop safely",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.PLANNING,
        turns=[ConversationTurn(index=1, request=rotated_credential)],
        messages=[{"role": "user", "content": rotated_credential}],
    )
    storage.create_run(
        run,
        instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
    )
    manager = AgentManager(
        replace(settings, api_key=rotated_credential),
        storage,
        ScriptedProvider([]),
    )

    def reject_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise SecureCheckpointError("reader is busy")

    monkeypatch.setattr(storage, "secure_checkpoint", reject_checkpoint)
    cancelled = await manager.cancel(run.id)

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.provider_reasoning_cleanup_pending is True
    assert cancelled.messages == []
    assert rotated_credential not in cancelled.model_dump_json()
    assert storage.has_active_run(settings.workspace) is False
    with pytest.raises(InvalidRunAction, match="cleanup is still waiting"):
        await manager.follow_up(run.id, "Continue after cleanup")
    persisted = storage.get_run(run.id)
    assert persisted.state is RunState.CANCELLED
    assert persisted.provider_reasoning_cleanup_pending is True


@pytest.mark.asyncio
async def test_accepted_clarification_crash_window_pairs_answer_exactly_once(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider([_question_response("question-call")]),
    )
    run = await first.start_run("Clarify durably")
    await _wait_for_state(storage, run.id, RunState.AWAITING_CLARIFICATION)
    receipt = storage.get_active_decision(run.id)
    assert receipt is not None
    answers = [ClarificationAnswer(question_id="choice", option_id="a")]
    storage.accept_decision(
        run.id,
        receipt.request_id,
        DecisionKind.CLARIFICATION,
        {"answers": [answer.model_dump(mode="json") for answer in answers]},
    )

    detached = storage.get_run(run.id)
    first._append_clarification_answers(detached, answers, "question-call")
    assert not any(
        message.get("role") == "tool"
        and message.get("tool_call_id") == "question-call"
        for message in storage.get_run(run.id).messages
    )
    await first.shutdown()

    second = AgentManager(
        settings,
        storage,
        ScriptedProvider([_direct_response("Clarification applied")]),
    )
    await second.resume(run.id)
    completed = await second.wait(run.id)

    assert completed.state is RunState.ANSWERED
    paired_answers = [
        message
        for message in completed.messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "question-call"
    ]
    assert len(paired_answers) == 1
    assert "Clarification answers" not in str(completed.messages)


@pytest.mark.asyncio
async def test_restarted_clarification_rounds_ignore_reopen_events_and_reused_call_ids(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider([_question_response("reused-question", prompt="First choice?")]),
    )
    run = await first.start_run("Clarify twice across restarts")
    await _wait_for_state(storage, run.id, RunState.AWAITING_CLARIFICATION)
    first_request = storage.get_active_decision(run.id)
    assert first_request is not None
    await first.shutdown()

    second = AgentManager(
        settings,
        storage,
        ScriptedProvider([_question_response("reused-question", prompt="Second choice?")]),
    )
    await second.resume(run.id)
    await _wait_for_state(storage, run.id, RunState.AWAITING_CLARIFICATION)
    assert storage.get_active_decision(run.id) == first_request
    await second.answer_clarification(
        run.id,
        [ClarificationAnswer(question_id="choice", option_id="a")],
        request_id=first_request.request_id,
    )
    async with asyncio.timeout(3):
        while True:
            waiting = storage.get_run(run.id)
            second_request = storage.get_active_decision(run.id)
            if (
                waiting.state is RunState.AWAITING_CLARIFICATION
                and waiting.clarification is not None
                and waiting.clarification.round == 2
                and second_request is not None
                and second_request.request_id != first_request.request_id
            ):
                break
            await asyncio.sleep(0.01)
    await second.shutdown()

    third = AgentManager(
        settings,
        storage,
        ScriptedProvider([_direct_response("Both rounds applied")]),
    )
    await third.resume(run.id)
    await _wait_for_state(storage, run.id, RunState.AWAITING_CLARIFICATION)
    reopened_second = storage.get_active_decision(run.id)
    assert reopened_second is not None
    assert reopened_second.request_id == second_request.request_id
    await third.answer_clarification(
        run.id,
        [ClarificationAnswer(question_id="choice", option_id="b")],
        request_id=reopened_second.request_id,
    )
    completed = await third.wait(run.id)

    assert completed.state is RunState.ANSWERED
    request_events = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.CLARIFICATION_REQUESTED
    ]
    assert len(request_events) == 4
    assert len({event.payload["request_id"] for event in request_events}) == 2
    assert sum(
        message.get("role") == "tool"
        and message.get("tool_call_id") == "reused-question"
        for message in completed.messages
    ) == 2


@pytest.mark.asyncio
async def test_accepted_plan_revision_after_restart_pairs_submit_plan_call(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="plan-call",
                            name="submit_plan",
                            arguments=_review_plan(),
                        )
                    ]
                )
            ]
        ),
    )
    run = await first.start_run(
        "Revise durably",
        verifier_enabled=False,
        mode=InteractionMode.PLAN,
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    receipt = storage.get_active_decision(run.id)
    assert receipt is not None
    storage.accept_decision(
        run.id,
        receipt.request_id,
        DecisionKind.PLAN,
        {"decision": "revise", "feedback": "Narrow the scope"},
    )
    await first.shutdown()

    revised_plan = {**_review_plan(), "summary": "Narrow revised plan"}
    second_provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="revised-plan",
                        name="submit_plan",
                        arguments=revised_plan,
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-revised",
                        name="finish",
                        arguments={"summary": "Applied revised plan"},
                    )
                ]
            ),
        ]
    )
    second = AgentManager(settings, storage, second_provider)
    await second.resume(run.id)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)

    planner_messages = second_provider.requests[0][0]
    paired = [
        message
        for message in planner_messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "plan-call"
    ]
    assert len(paired) == 1
    assert "Narrow the scope" in str(paired[0].get("content"))
    revised_request = storage.get_active_decision(run.id)
    assert revised_request is not None
    await second.decide_plan(
        run.id,
        PlanDecision(decision="approve"),
        request_id=revised_request.request_id,
    )
    assert (await second.wait(run.id)).state is RunState.SUCCEEDED


@pytest.mark.asyncio
async def test_rejected_action_result_survives_crash_after_decision_consumption(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                    ]
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="rejected-action",
                            name="run_command",
                            arguments={"argv": ["python", "app.py"]},
                        )
                    ]
                ),
            ]
        ),
    )
    run = await first.start_run(
        "Reject durably",
        verifier_enabled=False,
        approval_mode=ApprovalMode.MANUAL,
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    waiting = storage.get_run(run.id)
    approval = waiting.pending_approval
    assert approval is not None
    storage.accept_decision(
        run.id,
        approval.id,
        DecisionKind.ACTION,
        {"approved": False},
    )
    permission = first.tools.resolve_permission(
        approval.tool_call,
        waiting.plan,
        waiting.approval_mode,
    )
    persisted_result = await first._consume_action_decision(
        waiting,
        approval,
        permission,
        False,
    )
    assert persisted_result is not None
    await first.shutdown()

    second = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish-after-rejection",
                            name="finish",
                            arguments={"summary": "Preserved rejection"},
                        )
                    ]
                )
            ]
        ),
    )
    await second.resume(run.id)
    completed = await second.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    rejected_messages = [
        message
        for message in completed.messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "rejected-action"
    ]
    assert len(rejected_messages) == 1
    assert "User rejected this action" in str(rejected_messages[0]["content"])
    assert "stopped before this tool call completed" not in str(completed.messages)
    completed_events = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.TOOL_COMPLETED
        and event.payload.get("approval_request_id") == approval.id
    ]
    assert len(completed_events) == 1


@pytest.mark.asyncio
async def test_accepted_plan_survives_crash_before_worker_notification(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                    ]
                )
            ]
        ),
    )
    run = await first.start_run(
        "Resume an accepted plan", verifier_enabled=False, mode=InteractionMode.PLAN
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    receipt = storage.get_active_decision(run.id)
    assert receipt is not None
    storage.accept_decision(
        run.id,
        receipt.request_id,
        DecisionKind.PLAN,
        {"decision": "approve", "feedback": ""},
    )
    await first.shutdown()

    interrupted = storage.get_run(run.id)
    assert interrupted.state is RunState.INTERRUPTED
    assert storage.get_active_decision(run.id) is not None
    second = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"summary": "Finished after durable approval"},
                        )
                    ]
                )
            ]
        ),
    )
    await second.resume(run.id)
    completed = await second.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    consumed = storage.get_decision(run.id, receipt.request_id)
    assert consumed.status is DecisionStatus.CONSUMED
    resumed = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.RUN_RESUMED
    )
    assert resumed.payload["strategy"] == "consume_accepted_plan"


@pytest.mark.asyncio
async def test_accepted_action_before_crash_executes_once_after_explicit_resume(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider(
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
                            arguments={"path": "resumed.txt", "content": "once\n"},
                        )
                    ]
                ),
            ]
        ),
    )
    run = await first.start_run(
        "Create once",
        verifier_enabled=False,
        approval_mode=ApprovalMode.MANUAL,
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    approval = storage.get_run(run.id).pending_approval
    assert approval is not None
    storage.accept_decision(
        run.id,
        approval.id,
        DecisionKind.ACTION,
        {"approved": True},
    )
    await first.shutdown()
    assert not (settings.workspace / "resumed.txt").exists()

    second = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"summary": "Created once after resume"},
                        )
                    ]
                )
            ]
        ),
    )
    await second.resume(run.id)
    completed = await second.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    assert (settings.workspace / "resumed.txt").read_text() == "once\n"
    events = storage.get_events(run.id)
    assert sum(
        event.type is EventType.TOOL_STARTED and event.payload.get("id") == "create"
        for event in events
    ) == 1
    receipt = storage.get_decision(run.id, approval.id)
    assert receipt.status is DecisionStatus.CONSUMED
    assert receipt.execution_started_at is not None


@pytest.mark.asyncio
async def test_started_approved_action_is_uncertain_and_never_replayed_after_crash(
    settings: Settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute_started = asyncio.Event()
    never_release = asyncio.Event()
    execute_calls = 0
    first = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="write", name="list_files", arguments={})
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
                            id="write",
                            name="create_file",
                            arguments={"path": "must-not-replay.txt", "content": "x\n"},
                        )
                    ]
                ),
            ]
        ),
    )
    original_execute = first.tools.execute

    async def block_after_start(
        run_id: str,
        call: ToolCall,
        *,
        sandbox_bypass: bool = False,
    ) -> ToolResult:
        nonlocal execute_calls
        if call.name != "create_file":
            return await original_execute(
                run_id,
                call,
                sandbox_bypass=sandbox_bypass,
            )
        execute_calls += 1
        execute_started.set()
        await never_release.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(first.tools, "execute", block_after_start)
    run = await first.start_run(
        "Do not replay",
        verifier_enabled=False,
        approval_mode=ApprovalMode.MANUAL,
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    approval = storage.get_run(run.id).pending_approval
    assert approval is not None
    await first.decide_action(run.id, approval.id, approved=True)
    await asyncio.wait_for(execute_started.wait(), timeout=3)
    await first.shutdown()
    assert execute_calls == 1

    second = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"summary": "Inspected uncertain action"},
                        )
                    ]
                )
            ]
        ),
    )
    await second.resume(run.id)
    completed = await second.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    assert execute_calls == 1
    assert not (settings.workspace / "must-not-replay.txt").exists()
    uncertain = storage.get_decision(run.id, approval.id)
    assert uncertain.status is DecisionStatus.UNCERTAIN
    resumed = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.RUN_RESUMED
    )
    assert resumed.payload["uncertain_action_approvals"] == 1


@pytest.mark.asyncio
async def test_user_cancel_marks_a_started_approved_action_uncertain(
    settings: Settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute_started = asyncio.Event()
    never_release = asyncio.Event()
    manager = AgentManager(
        settings,
        storage,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=_review_plan())
                    ]
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="write",
                            name="create_file",
                            arguments={"path": "cancelled-action.txt", "content": "x\n"},
                        )
                    ]
                ),
            ]
        ),
    )

    async def block_after_start(*_args: object, **_kwargs: object) -> ToolResult:
        execute_started.set()
        await never_release.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(manager.tools, "execute", block_after_start)
    run = await manager.start_run(
        "Cancel after action start",
        verifier_enabled=False,
        approval_mode=ApprovalMode.MANUAL,
    )
    await _wait_for_state(storage, run.id, RunState.AWAITING_ACTION_APPROVAL)
    approval = storage.get_run(run.id).pending_approval
    assert approval is not None
    await manager.decide_action(run.id, approval.id, approved=True)
    await asyncio.wait_for(execute_started.wait(), timeout=3)

    cancelled = await manager.cancel(run.id)

    assert cancelled.state is RunState.CANCELLED
    receipt = storage.get_decision(run.id, approval.id)
    assert receipt.status is DecisionStatus.UNCERTAIN
    assert receipt.execution_started_at is not None
    assert not (settings.workspace / "cancelled-action.txt").exists()


@pytest.mark.asyncio
async def test_shutdown_resume_plan_then_rollback(
    settings: Settings, storage: Storage
) -> None:
    first = AgentManager(settings, storage, ScriptedProvider([
        ModelResponse(
            tool_calls=[ToolCall(id="plan", name="submit_plan", arguments=_review_plan())]
        )
    ]))
    run = await first.start_run(
        "Resume me", verifier_enabled=False, mode=InteractionMode.PLAN
    )
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
                        arguments={"summary": "Resumed"},
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
async def test_transient_model_outage_pauses_and_resumes_without_losing_run(
    settings: Settings, storage: Storage
) -> None:
    low_risk_plan = {
        "summary": "Review one local note",
        "steps": [{"id": "review", "title": "Review the note"}],
        "acceptance_checks": [{"id": "reviewed", "label": "The note is reviewed"}],
        "impacted_files": ["note.txt"],
    }

    class RecoveringProvider:
        def __init__(self) -> None:
            self.outcomes = [
                ProviderError("network unavailable", retryable=True, category="connection"),
                ProviderError("network unavailable", retryable=True, category="connection"),
                ProviderError("network unavailable", retryable=True, category="connection"),
                ModelResponse(
                    tool_calls=[
                        ToolCall(id="plan", name="submit_plan", arguments=low_risk_plan)
                    ]
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"summary": "Reviewed"},
                        )
                    ]
                ),
            ]

        async def complete(self, messages, tools=None) -> ModelResponse:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, ProviderError):
                raise outcome
            return outcome

    resilient = replace(settings, model_retry_delay=0)
    manager = AgentManager(resilient, storage, RecoveringProvider())
    run = await manager.start_run("Review note.txt", verifier_enabled=False)

    interrupted = await manager.wait(run.id)

    assert interrupted.state is RunState.INTERRUPTED
    assert interrupted.interrupted_from is RunState.PLANNING
    assert "preserved" in (interrupted.error or "")
    retries = [
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.MODEL_RETRY
    ]
    assert [event.payload["next_attempt"] for event in retries] == [2, 3]
    failure = next(
        event for event in storage.get_events(run.id) if event.type is EventType.ERROR
    )
    assert failure.payload["recoverable"] is True
    assert failure.payload["cause"] == "model_unavailable"

    await manager.resume(run.id)
    completed = await manager.wait(run.id)

    assert completed.state is RunState.SUCCEEDED, completed.error
    assert completed.error is None
    assert completed.plan_gate is not None
    assert completed.plan_gate.decision == "agent_continues"


@pytest.mark.asyncio
async def test_cancel_reloads_run_after_provider_interrupts_during_tool_cleanup(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ControlledOutageProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, _messages, _tools=None) -> ModelResponse:
            self.started.set()
            await self.release.wait()
            raise ProviderError(
                "transport became unavailable",
                retryable=True,
                category="connection",
            )

    provider = ControlledOutageProvider()
    manager = AgentManager(
        replace(settings, model_retry_attempts=1, model_retry_delay=0),
        storage,
        provider,
    )
    run = await manager.start_run("cancel while the provider interruption wins")
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    tool_cancel_started = asyncio.Event()
    release_tool_cancel = asyncio.Event()

    async def delayed_tool_cancel(_run_id: str) -> None:
        tool_cancel_started.set()
        await release_tool_cancel.wait()

    monkeypatch.setattr(manager.tools, "cancel", delayed_tool_cancel)
    cancel_task = asyncio.create_task(manager.cancel(run.id))
    await asyncio.wait_for(tool_cancel_started.wait(), timeout=2)
    provider.release.set()
    await _wait_for_state(storage, run.id, RunState.INTERRUPTED)
    await manager.wait(run.id)
    release_tool_cancel.set()

    cancelled = await asyncio.wait_for(cancel_task, timeout=2)
    events = storage.get_events(run.id)
    completed = [event for event in events if event.type is EventType.RUN_COMPLETED]

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.turns[-1].outcome == "cancelled"
    assert len(completed) == 1
    assert completed[0].payload["state"] == RunState.CANCELLED.value


@pytest.mark.asyncio
async def test_unexpected_provider_exception_never_leaves_a_ghost_run(
    settings: Settings, storage: Storage
) -> None:
    class BrokenProvider:
        async def complete(self, messages, tools=None) -> ModelResponse:
            raise IndexError("malformed provider response")

    manager = AgentManager(settings, storage, BrokenProvider())
    run = await manager.start_run("Do not stay active after a provider crash")

    completed = await manager.wait(run.id)

    assert completed.state is RunState.FAILED
    assert "provider failed unexpectedly (IndexError)" in (completed.error or "")
    assert storage.has_live_run() is False
    assert storage.get_events(run.id)[-1].type is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_resume_repairs_only_the_missing_result_in_a_partial_tool_batch(
    settings: Settings, storage: Storage
) -> None:
    interrupted_call = ModelResponse(
        tool_calls=[
            ToolCall(
                id="known-complete",
                name="create_file",
                arguments={"path": "known.txt", "content": "already handled\n"},
            ),
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
        messages=[
            interrupted_call,
            {
                "role": "tool",
                "tool_call_id": "known-complete",
                "name": "create_file",
                "content": ToolResult(
                    tool_call_id="known-complete",
                    name="create_file",
                    ok=True,
                    output="The first call already completed before interruption.",
                ).model_dump_json(),
            },
        ],
    )
    storage.create_run(
        run,
        instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={
                            "summary": "Recovered after inspection",
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
    assert not (settings.workspace / "known.txt").exists()
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
    tool_messages = [
        message for message in first_request if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "known-complete",
        "unknown-side-effect",
    ]
    synthetic = tool_messages[1]
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
    storage.create_run(
        run,
        instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
    )
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
                            arguments={"summary": "Resumed"},
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
    run = await manager.start_run("Cancel me", mode=InteractionMode.PLAN)
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
    assert messages == []
    persisted = json.dumps(
        storage.get_run(run.id).model_dump(mode="json"), ensure_ascii=False
    )
    assert settings.api_key not in persisted
    assert redact_text(settings.api_key, api_key=settings.api_key) in persisted


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
                        arguments={"summary": "Ready"},
                    )
                ]
            ),
            mixed_review,
            _verification("pass", "The focused read confirms the result."),
        ]
    )
    manager = AgentManager(settings, storage, provider)
    run = await manager.start_run("Review evidence", mode=InteractionMode.PLAN)
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
        await manager.decide_action("terminal", "missing", approved=True)
    with pytest.raises(InvalidRunAction, match="interrupted"):
        await manager.resume("terminal")

    active = RunRecord(
        id="active",
        task="Active",
        workspace=str(settings.workspace),
        state=RunState.EXECUTING,
    )
    storage.create_run(active)
    with pytest.raises(InvalidRunAction, match="current turn"):
        await manager.follow_up("active", "Continue")
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
                        arguments={"summary": "Ready"},
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
    run = await manager.start_run("Recover verifier output", mode=InteractionMode.PLAN)
    await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
    await manager.decide_plan(run.id, PlanDecision(decision="approve"))

    completed = await manager.wait(run.id)
    assert completed.state is RunState.SUCCEEDED
    final_request = provider.requests[-1][0]
    assert any(
        "Invalid verification report" in (message.get("content") or "")
        for message in final_request
    )
