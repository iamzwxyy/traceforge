from __future__ import annotations

import asyncio

import pytest

from traceforge.agent import AgentManager, PlanDecision
from traceforge.config import Settings
from traceforge.models import (
    ClarificationAnswer,
    EventType,
    RunState,
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

    assert completed.state is RunState.SUCCEEDED
    assert completed.verification is not None
    assert completed.verification.verdict is Verdict.PASS
    assert (settings.workspace / "result.txt").read_text() == "hello\n"
    event_types = [event.type for event in storage.get_events(run.id)]
    assert EventType.CLARIFICATION_REQUESTED in event_types
    assert EventType.DIFF_UPDATED in event_types
    assert EventType.VERIFICATION_COMPLETED in event_types


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
