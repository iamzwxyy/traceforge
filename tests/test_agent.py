from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from traceforge.agent import AgentManager, InvalidRunAction, PlanDecision, RunConflictError
from traceforge.config import Settings
from traceforge.models import (
    ApprovalMode,
    ClarificationAnswer,
    EventType,
    InteractionMode,
    PlanGate,
    ReasoningEffort,
    RunRecord,
    RunState,
    TaskPlan,
    ToolCall,
    ToolResult,
    Verdict,
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
        if message.get("role") == "tool" and '"ok":false' in str(message.get("content"))
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
    assert "[REDACTED]" in first.turns[-1].summary

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
                        arguments={"summary": "Reviewed the first request"},
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
                        arguments={"summary": "Applied the follow-up review"},
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
                        arguments={"summary": "Created a.txt", "evidence": ["diff"]},
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
                        arguments={"summary": "Updated both files", "evidence": ["diff"]},
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
                            "evidence": ["check"],
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
                        arguments={"summary": "Created note", "evidence": ["diff"]},
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
                        arguments={"summary": "Observed", "evidence": ["command was rejected"]},
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
                        arguments={"summary": "Reviewed", "evidence": ["plan"]},
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
                        arguments={"summary": "Reviewed", "evidence": ["plan"]},
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
                        arguments={"summary": "Reviewed", "evidence": ["plan"]},
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
    run = await manager.start_run(
        "Review context.txt", verifier_enabled=False, mode=InteractionMode.PLAN
    )
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
                        arguments={"summary": "Done", "evidence": ["claimed"]},
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
                        arguments={"summary": "Approved", "evidence": ["check"]},
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
                        arguments={"summary": "Created", "evidence": ["diff"]},
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
                        arguments={"summary": "Ran safely", "evidence": ["sandbox"]},
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
async def test_restart_abandons_pending_approval_without_replaying_action(
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
    assert interrupted.pending_approval is None
    assert interrupted.interrupted_from is RunState.AWAITING_ACTION_APPROVAL
    abandoned = next(
        event
        for event in storage.get_events(run.id)
        if event.type is EventType.APPROVAL_RESOLVED
        and event.payload.get("outcome") == "abandoned"
    )
    assert abandoned.payload["approval_id"] == approval.id
    assert abandoned.payload["cause"] == "process_shutdown"

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
                                "evidence": ["no replay"],
                            },
                        )
                    ]
                )
            ]
        ),
    )
    await second.resume(run.id)
    completed = await second.wait(run.id)

    assert completed.state is RunState.SUCCEEDED
    assert not any(
        event.type is EventType.TOOL_COMPLETED
        and event.payload["call"]["id"] == "pending-command"
        for event in storage.get_events(run.id)
    )


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
                            arguments={"summary": "Reviewed", "evidence": ["workspace"]},
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
    persisted = json.dumps(storage.get_run(run.id).model_dump(mode="json"))
    assert settings.api_key not in persisted
    assert "[REDACTED]" in persisted


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
