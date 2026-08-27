from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from traceforge.config import Settings
from traceforge.context import ContextManager
from traceforge.events import EventBroker
from traceforge.models import (
    ApprovalRequest,
    CheckStatus,
    ClarificationAnswer,
    ClarificationRequest,
    EventType,
    RunRecord,
    RunState,
    TaskPlan,
    ToolCall,
    ToolResult,
    Verdict,
    VerificationReport,
)
from traceforge.prompts import BUILDER_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, VERIFIER_SYSTEM_PROMPT
from traceforge.provider import ModelProvider, ModelResponse, ProviderError
from traceforge.storage import Storage
from traceforge.tools import PermissionAssessment, PermissionDecision, ToolRegistry
from traceforge.workspace import RollbackResult, Workspace


class RunConflictError(RuntimeError):
    pass


class InvalidRunAction(RuntimeError):
    pass


class PlanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "revise"]
    feedback: str = Field(default="", max_length=2_000)


@dataclass(slots=True)
class _Control:
    clarification_future: asyncio.Future[list[ClarificationAnswer]] | None = None
    plan_future: asyncio.Future[PlanDecision] | None = None
    approval_future: asyncio.Future[bool] | None = None


_ALLOWED_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.CREATED: {
        RunState.PLANNING,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.INTERRUPTED,
    },
    RunState.PLANNING: {
        RunState.AWAITING_CLARIFICATION,
        RunState.AWAITING_PLAN_APPROVAL,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.INTERRUPTED,
    },
    RunState.AWAITING_CLARIFICATION: {
        RunState.PLANNING,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    },
    RunState.AWAITING_PLAN_APPROVAL: {
        RunState.PLANNING,
        RunState.EXECUTING,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    },
    RunState.EXECUTING: {
        RunState.AWAITING_ACTION_APPROVAL,
        RunState.VERIFYING,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.INTERRUPTED,
    },
    RunState.AWAITING_ACTION_APPROVAL: {
        RunState.EXECUTING,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.INTERRUPTED,
    },
    RunState.VERIFYING: {
        RunState.EXECUTING,
        RunState.SUCCEEDED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.INTERRUPTED,
    },
    RunState.INTERRUPTED: {
        RunState.PLANNING,
        RunState.AWAITING_CLARIFICATION,
        RunState.AWAITING_PLAN_APPROVAL,
        RunState.EXECUTING,
        RunState.CANCELLED,
        RunState.ROLLED_BACK,
    },
    RunState.SUCCEEDED: {RunState.ROLLED_BACK},
    RunState.FAILED: {RunState.ROLLED_BACK},
    RunState.CANCELLED: {RunState.ROLLED_BACK},
    RunState.ROLLED_BACK: set(),
}


class AgentManager:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        provider: ModelProvider,
        *,
        broker: EventBroker | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.provider = provider
        self.workspace = Workspace(settings.workspace, storage)
        self.tools = ToolRegistry(self.workspace, settings)
        self.context = ContextManager(settings.context_limit)
        self.broker = broker or EventBroker(storage)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._controls: dict[str, _Control] = {}
        self._shutting_down = False
        self.storage.mark_active_runs_interrupted(settings.workspace)

    async def start_run(self, task: str, *, verifier_enabled: bool = True) -> RunRecord:
        clean_task = task.strip()
        if not clean_task:
            raise ValueError("Task must not be empty")
        if self.storage.has_active_run(self.settings.workspace):
            raise RunConflictError("This workspace already has an active or interrupted run")
        run = RunRecord(
            id=uuid4().hex,
            task=clean_task,
            workspace=str(self.settings.workspace),
            verifier_enabled=verifier_enabled,
        )
        self.storage.create_run(run)
        self._controls[run.id] = _Control()
        await self.broker.emit(
            run.id,
            EventType.STATE_CHANGED,
            {"state": run.state.value, "previous": None},
        )
        self._spawn(run.id, resume=False)
        return run

    async def answer_clarification(
        self, run_id: str, answers: list[ClarificationAnswer]
    ) -> None:
        run = self.storage.get_run(run_id)
        if run.state is not RunState.AWAITING_CLARIFICATION or run.clarification is None:
            raise InvalidRunAction("The run is not waiting for clarification")
        expected = {question.id: question for question in run.clarification.questions}
        supplied = {answer.question_id: answer for answer in answers}
        if supplied.keys() != expected.keys():
            raise InvalidRunAction("Every clarification question must be answered exactly once")
        for question_id, answer in supplied.items():
            if answer.option_id and answer.option_id not in {
                option.id for option in expected[question_id].options
            }:
                raise InvalidRunAction(f"Unknown option for {question_id}: {answer.option_id}")
        future = self._control(run_id).clarification_future
        if future is None or future.done():
            raise InvalidRunAction("Clarification response window is not active")
        future.set_result(answers)

    async def decide_plan(self, run_id: str, decision: PlanDecision) -> None:
        run = self.storage.get_run(run_id)
        if run.state is not RunState.AWAITING_PLAN_APPROVAL:
            raise InvalidRunAction("The run is not waiting for plan approval")
        future = self._control(run_id).plan_future
        if future is None or future.done():
            raise InvalidRunAction("Plan decision window is not active")
        future.set_result(decision)

    async def decide_action(self, run_id: str, *, approved: bool) -> None:
        run = self.storage.get_run(run_id)
        if run.state is not RunState.AWAITING_ACTION_APPROVAL:
            raise InvalidRunAction("The run is not waiting for an action approval")
        future = self._control(run_id).approval_future
        if future is None or future.done():
            raise InvalidRunAction("Action decision window is not active")
        future.set_result(approved)

    async def cancel(self, run_id: str) -> RunRecord:
        run = self.storage.get_run(run_id)
        if run.state.terminal:
            return run
        await self.tools.cancel(run_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        elif run.state is RunState.INTERRUPTED:
            await self._transition(run, RunState.CANCELLED)
        return self.storage.get_run(run_id)

    async def resume(self, run_id: str) -> RunRecord:
        run = self.storage.get_run(run_id)
        if run.state is not RunState.INTERRUPTED:
            raise InvalidRunAction("Only interrupted runs can be resumed")
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise RunConflictError("Run is already active")
        self._controls[run_id] = _Control()
        self._spawn(run_id, resume=True)
        return run

    async def rollback(self, run_id: str) -> RollbackResult:
        run = self.storage.get_run(run_id)
        if not run.state.terminal and run.state is not RunState.INTERRUPTED:
            raise InvalidRunAction("Cancel the active run before rolling it back")
        result = self.workspace.rollback(run_id)
        await self._transition(run, RunState.ROLLED_BACK)
        await self.broker.emit(run_id, EventType.ROLLBACK_COMPLETED, _rollback_payload(result))
        return result

    async def wait(self, run_id: str) -> RunRecord:
        task = self._tasks.get(run_id)
        if task:
            await task
        return self.storage.get_run(run_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        active = [(run_id, task) for run_id, task in self._tasks.items() if not task.done()]
        for run_id, _task in active:
            await self.tools.cancel(run_id)
        for _run_id, task in active:
            task.cancel()
        if active:
            await asyncio.gather(*(task for _, task in active), return_exceptions=True)

    def _spawn(self, run_id: str, *, resume: bool) -> None:
        task = asyncio.create_task(self._run(run_id, resume=resume), name=f"traceforge:{run_id}")
        self._tasks[run_id] = task

    async def _run(self, run_id: str, *, resume: bool) -> None:
        run = self.storage.get_run(run_id)
        try:
            if resume:
                await self._prepare_resume(run)
            if not run.plan_approved:
                await self._planning_phase(run)
            if run.state is not RunState.EXECUTING:
                return
            while True:
                await self._builder_phase(run)
                run = self.storage.get_run(run_id)
                if run.state is not RunState.VERIFYING:
                    return
                report = await self._verifier_phase(run)
                run.verification = report
                self.storage.save_run(run)
                await self.broker.emit(
                    run.id,
                    EventType.VERIFICATION_COMPLETED,
                    report.model_dump(mode="json"),
                )
                if report.verdict is Verdict.PASS or not run.verifier_enabled:
                    if run.plan:
                        for check in run.plan.acceptance_checks:
                            if check.command is None and report.verdict is Verdict.PASS:
                                check.status = CheckStatus.PASSED
                                check.evidence = "Confirmed by the independent verifier."
                    await self._complete(run, report)
                    return
                if run.repair_cycles >= self.settings.max_repair_cycles:
                    await self._fail(
                        run,
                        "Independent verification did not pass after the allowed repair cycles.",
                    )
                    return
                run.repair_cycles += 1
                run.messages.append(
                    {
                        "role": "system",
                        "content": (
                            "The independent verifier rejected the current result. Address these "
                            f"findings and rerun acceptance checks:\n{report.model_dump_json()}"
                        ),
                    }
                )
                await self._transition(run, RunState.EXECUTING)
        except asyncio.CancelledError:
            current = self.storage.get_run(run_id)
            if not current.state.terminal:
                if self._shutting_down:
                    current.interrupted_from = current.state
                    await self._transition(current, RunState.INTERRUPTED)
                else:
                    await self._transition(current, RunState.CANCELLED)
                    await self.broker.emit(
                        run_id, EventType.RUN_COMPLETED, {"state": RunState.CANCELLED.value}
                    )
            raise
        except (ProviderError, ValidationError, ValueError, RuntimeError) as exc:
            current = self.storage.get_run(run_id)
            if not current.state.terminal:
                await self._fail(current, self._redact(str(exc)))
        finally:
            self._controls.pop(run_id, None)

    async def _prepare_resume(self, run: RunRecord) -> None:
        previous = run.interrupted_from
        run.interrupted_from = None
        run.error = None
        self._repair_incomplete_tool_protocol(run)
        if run.clarification is not None and not run.plan_approved:
            await self._transition(run, RunState.AWAITING_CLARIFICATION)
            answers = await self._await_clarification(run)
            self._append_clarification_answers(run, answers)
            run.clarification = None
            await self._transition(run, RunState.PLANNING)
        elif run.plan is not None and not run.plan_approved:
            await self._transition(run, RunState.AWAITING_PLAN_APPROVAL)
            approved = await self._await_plan_decision(run)
            if approved:
                run.plan_approved = True
                run.messages = [
                    {"role": "system", "content": BUILDER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Original task:\n{run.task}\n\nApproved plan:\n"
                            f"{run.plan.model_dump_json()}"
                        ),
                    },
                ]
                await self._transition(run, RunState.EXECUTING)
        elif run.plan_approved:
            run.pending_approval = None
            run.messages.append(
                {
                    "role": "system",
                    "content": (
                        f"The previous process stopped during {previous or 'execution'}. "
                        "Inspect current state before issuing any action again."
                    ),
                }
            )
            await self._transition(run, RunState.EXECUTING)
        else:
            await self._transition(run, RunState.PLANNING)

    async def _planning_phase(self, run: RunRecord) -> None:
        if not run.messages or run.messages[0].get("content") != PLANNER_SYSTEM_PROMPT:
            run.messages = [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": run.task},
            ]
        if run.state is not RunState.PLANNING:
            await self._transition(run, RunState.PLANNING)
        exploration_tools = [
            schema
            for schema in self.tools.schemas
            if schema["function"]["name"] in {"list_files", "read_file", "search_text"}
        ]
        planning_tools = [
            *exploration_tools,
            _model_tool(
                "ask_questions", "Ask material clarification questions.", _questions_schema()
            ),
            _model_tool(
                "submit_plan", "Submit the implementation plan.", TaskPlan.model_json_schema()
            ),
        ]
        non_tool_responses = 0
        for _ in range(12):
            response = await self._complete_model(run, planning_tools)
            run.messages.append(response.as_assistant_message())
            self.storage.save_run(run)
            if response.content:
                await self._emit_message(run, response.content, phase="planning")
            if not response.tool_calls:
                non_tool_responses += 1
                if non_tool_responses >= 2:
                    raise RuntimeError("Planner did not submit a plan or clarification request")
                run.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Use ask_questions or submit_plan now; do not answer in prose only."
                        ),
                    }
                )
                continue
            for call in response.tool_calls:
                if call.name in {"list_files", "read_file", "search_text"}:
                    result = await self.tools.execute(run.id, call)
                    self._append_tool_result(run, result)
                    await self._emit_tool_result(run, call, result)
                    continue
                if call.name == "ask_questions":
                    round_number = self._clarification_round(run.id) + 1
                    if round_number > 2:
                        self._append_tool_error(
                            run,
                            call,
                            "At most two clarification rounds are allowed. Submit a plan.",
                        )
                        continue
                    request = ClarificationRequest(
                        questions=call.arguments.get("questions", []), round=round_number
                    )
                    run.clarification = request
                    self.storage.save_run(run)
                    await self._transition(run, RunState.AWAITING_CLARIFICATION)
                    answers = await self._await_clarification(run)
                    self._append_clarification_answers(run, answers, call.id)
                    run.clarification = None
                    await self._transition(run, RunState.PLANNING)
                    continue
                if call.name == "submit_plan":
                    plan = TaskPlan.model_validate(call.arguments)
                    run.plan = plan
                    self.storage.save_run(run)
                    await self._transition(run, RunState.AWAITING_PLAN_APPROVAL)
                    approved = await self._await_plan_decision(run, tool_call_id=call.id)
                    if approved:
                        run.plan_approved = True
                        run.messages = [
                            {"role": "system", "content": BUILDER_SYSTEM_PROMPT},
                            {
                                "role": "user",
                                "content": (
                                    f"Original task:\n{run.task}\n\nApproved plan:\n"
                                    f"{plan.model_dump_json()}"
                                ),
                            },
                        ]
                        await self._transition(run, RunState.EXECUTING)
                        return
            self.storage.save_run(run)
        raise RuntimeError("Planning exceeded the maximum number of model turns")

    async def _builder_phase(self, run: RunRecord) -> None:
        repeated_failures: dict[str, int] = {}
        no_tool_responses = 0
        while run.step_count < self.settings.max_steps:
            response = await self._complete_model(run, self.tools.schemas)
            run.messages.append(response.as_assistant_message())
            if response.content:
                await self._emit_message(run, response.content, phase="building")
            if not response.tool_calls:
                no_tool_responses += 1
                if no_tool_responses >= 2:
                    raise RuntimeError("Builder stopped without calling finish")
                run.messages.append(
                    {
                        "role": "user",
                        "content": "Continue with tools, or call finish with concrete evidence.",
                    }
                )
                self.storage.save_run(run)
                continue
            no_tool_responses = 0
            for call in response.tool_calls:
                if call.name == "finish":
                    missing = self._missing_command_checks(run.plan)
                    if missing:
                        self._append_tool_error(
                            run,
                            call,
                            "Command checks need fresh passing evidence: " + ", ".join(missing),
                        )
                        continue
                    self._append_tool_result(
                        run,
                        ToolResult(
                            tool_call_id=call.id,
                            name=call.name,
                            ok=True,
                            output="Completion request accepted for independent verification.",
                        ),
                    )
                    await self._transition(run, RunState.VERIFYING)
                    return
                run.step_count += 1
                if run.step_count > self.settings.max_steps:
                    break
                await self.broker.emit(
                    run.id, EventType.TOOL_REQUESTED, call.model_dump(mode="json")
                )
                assessment = self.tools.assess(call, run.plan)
                if assessment.decision is PermissionDecision.DENY:
                    result = ToolResult(
                        tool_call_id=call.id,
                        name=call.name,
                        ok=False,
                        error=assessment.reason,
                    )
                else:
                    if assessment.decision is PermissionDecision.ASK:
                        approved = await self._await_action_approval(run, call, assessment)
                        if not approved:
                            result = ToolResult(
                                tool_call_id=call.id,
                                name=call.name,
                                ok=False,
                                error="User rejected this action.",
                            )
                            self._append_tool_result(run, result)
                            await self._emit_tool_result(run, call, result)
                            continue
                    await self._transition(run, RunState.EXECUTING)
                    await self.broker.emit(
                        run.id, EventType.TOOL_STARTED, call.model_dump(mode="json")
                    )
                    result = await self.tools.execute(run.id, call)
                result = self._redact_result(result)
                self._append_tool_result(run, result)
                await self._update_checks_and_diff(run, call, result)
                await self._emit_tool_result(run, call, result)
                if not result.ok:
                    fingerprint = json.dumps(
                        {"name": call.name, "arguments": call.arguments}, sort_keys=True
                    )
                    repeated_failures[fingerprint] = repeated_failures.get(fingerprint, 0) + 1
                    if repeated_failures[fingerprint] == 2:
                        run.messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "The same action has failed twice. Enter recovery mode: "
                                    "inspect "
                                    "current state and choose a different action."
                                ),
                            }
                        )
                    elif repeated_failures[fingerprint] >= 3:
                        raise RuntimeError("The same tool call failed three times")
                self.storage.save_run(run)
        raise RuntimeError(f"Builder exceeded the {self.settings.max_steps}-step limit")

    async def _verifier_phase(self, run: RunRecord) -> VerificationReport:
        if not run.verifier_enabled:
            return VerificationReport(
                verdict=Verdict.INCONCLUSIVE,
                summary="Independent verification was disabled; command checks passed.",
            )
        assert run.plan is not None
        evidence = {
            "task": run.task,
            "plan": run.plan.model_dump(mode="json"),
            "diff": self.workspace.diff(run.id)[:60_000],
            "tool_events": [
                event.model_dump(mode="json")
                for event in self.storage.get_events(run.id)
                if event.type is EventType.TOOL_COMPLETED
            ][-20:],
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
        ]
        read_tools = [
            schema
            for schema in self.tools.schemas
            if schema["function"]["name"] in {"list_files", "read_file", "search_text"}
        ]
        verifier_tools = [
            *read_tools,
            _model_tool(
                "submit_verification",
                "Submit the independent verification verdict.",
                VerificationReport.model_json_schema(),
            ),
        ]
        for _ in range(8):
            prepared, _ = self.context.prepare(messages)
            response = await self.provider.complete(prepared, verifier_tools)
            messages.append(response.as_assistant_message())
            if response.content:
                await self._emit_message(run, response.content, phase="verifying")
            submit_calls = [
                call for call in response.tool_calls if call.name == "submit_verification"
            ]
            read_calls = [
                call for call in response.tool_calls if call.name != "submit_verification"
            ]
            if submit_calls and not read_calls:
                return VerificationReport.model_validate(submit_calls[0].arguments)
            for call in read_calls:
                if call.name not in {"list_files", "read_file", "search_text"}:
                    result = ToolResult(
                        tool_call_id=call.id,
                        name=call.name,
                        ok=False,
                        error="Verifier is read-only.",
                    )
                else:
                    result = await self.tools.execute(run.id, call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.tool_call_id,
                        "name": result.name,
                        "content": result.model_dump_json(),
                    }
                )
            if submit_calls and read_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Review the tool evidence, then submit the verdict in a new turn."
                        ),
                    }
                )
        return VerificationReport(
            verdict=Verdict.INCONCLUSIVE,
            summary="Verifier did not produce a valid structured verdict.",
        )

    async def _complete(self, run: RunRecord, report: VerificationReport) -> None:
        self.storage.save_run(run)
        await self._transition(run, RunState.SUCCEEDED)
        await self.broker.emit(
            run.id,
            EventType.RUN_COMPLETED,
            {
                "state": RunState.SUCCEEDED.value,
                "verification": report.model_dump(mode="json"),
                "diff": self.workspace.diff(run.id),
            },
        )

    async def _fail(self, run: RunRecord, error: str) -> None:
        run.error = error
        self.storage.save_run(run)
        await self.broker.emit(run.id, EventType.ERROR, {"message": error})
        await self._transition(run, RunState.FAILED)
        await self.broker.emit(
            run.id,
            EventType.RUN_COMPLETED,
            {"state": RunState.FAILED.value, "error": error},
        )

    async def _complete_model(
        self, run: RunRecord, tools: list[dict[str, Any]]
    ) -> ModelResponse:
        prepared, compacted = self.context.prepare(run.messages)
        if compacted:
            run.messages = prepared
            self.storage.save_run(run)
            await self.broker.emit(
                run.id,
                EventType.MESSAGE,
                {
                    "phase": "system",
                    "content": "Older execution history was compacted to protect context quality.",
                },
            )
        return await self.provider.complete(prepared, tools)

    async def _await_clarification(self, run: RunRecord) -> list[ClarificationAnswer]:
        future: asyncio.Future[list[ClarificationAnswer]] = (
            asyncio.get_running_loop().create_future()
        )
        self._control(run.id).clarification_future = future
        assert run.clarification is not None
        await self.broker.emit(
            run.id,
            EventType.CLARIFICATION_REQUESTED,
            run.clarification.model_dump(mode="json"),
        )
        answers = await future
        await self.broker.emit(
            run.id,
            EventType.CLARIFICATION_ANSWERED,
            {"answers": [answer.model_dump(mode="json") for answer in answers]},
        )
        return answers

    async def _await_plan_decision(
        self, run: RunRecord, *, tool_call_id: str | None = None
    ) -> bool:
        future: asyncio.Future[PlanDecision] = asyncio.get_running_loop().create_future()
        self._control(run.id).plan_future = future
        assert run.plan is not None
        await self.broker.emit(
            run.id, EventType.PLAN_UPDATED, run.plan.model_dump(mode="json")
        )
        decision = await future
        if decision.decision == "approve":
            if tool_call_id:
                self._append_tool_result(
                    run,
                    ToolResult(
                        tool_call_id=tool_call_id,
                        name="submit_plan",
                        ok=True,
                        output="The user approved this plan.",
                    ),
                )
            return True
        if tool_call_id:
            self._append_tool_result(
                run,
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="submit_plan",
                    ok=False,
                    error=f"The user requested a revision: {decision.feedback}",
                ),
            )
        run.plan = None
        await self._transition(run, RunState.PLANNING)
        return False

    async def _await_action_approval(
        self, run: RunRecord, call: ToolCall, assessment: PermissionAssessment
    ) -> bool:
        approval = ApprovalRequest(
            id=uuid4().hex,
            tool_call=call,
            summary=_command_summary(call),
            reason=assessment.reason,
            risk=assessment.risk,
        )
        run.pending_approval = approval
        self.storage.save_run(run)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._control(run.id).approval_future = future
        await self._transition(run, RunState.AWAITING_ACTION_APPROVAL)
        await self.broker.emit(
            run.id, EventType.APPROVAL_REQUESTED, approval.model_dump(mode="json")
        )
        approved = await future
        run.pending_approval = None
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.APPROVAL_RESOLVED,
            {"approval_id": approval.id, "approved": approved},
        )
        await self._transition(run, RunState.EXECUTING)
        return approved

    async def _transition(self, run: RunRecord, new_state: RunState) -> None:
        if run.state is new_state:
            self.storage.save_run(run)
            return
        if new_state not in _ALLOWED_TRANSITIONS[run.state]:
            raise RuntimeError(f"Invalid run transition: {run.state.value} -> {new_state.value}")
        previous = run.state
        run.state = new_state
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.STATE_CHANGED,
            {"state": new_state.value, "previous": previous.value},
        )

    def _append_clarification_answers(
        self,
        run: RunRecord,
        answers: list[ClarificationAnswer],
        tool_call_id: str | None = None,
    ) -> None:
        content = json.dumps(
            {"answers": [answer.model_dump(mode="json") for answer in answers]},
            ensure_ascii=False,
        )
        if tool_call_id:
            run.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": "ask_questions",
                    "content": content,
                }
            )
        else:
            run.messages.append({"role": "user", "content": f"Clarification answers: {content}"})
        self.storage.save_run(run)

    def _append_tool_result(self, run: RunRecord, result: ToolResult) -> None:
        run.messages.append(
            {
                "role": "tool",
                "tool_call_id": result.tool_call_id,
                "name": result.name,
                "content": result.model_dump_json(),
            }
        )

    def _append_tool_error(self, run: RunRecord, call: ToolCall, error: str) -> None:
        self._append_tool_result(
            run,
            ToolResult(tool_call_id=call.id, name=call.name, ok=False, error=error),
        )

    async def _emit_tool_result(
        self, run: RunRecord, call: ToolCall, result: ToolResult
    ) -> None:
        await self.broker.emit(
            run.id,
            EventType.TOOL_COMPLETED,
            {"call": call.model_dump(mode="json"), "result": result.model_dump(mode="json")},
        )

    async def _emit_message(self, run: RunRecord, content: str, *, phase: str) -> None:
        await self.broker.emit(
            run.id,
            EventType.MESSAGE,
            {"phase": phase, "content": self._redact(content)},
        )

    async def _update_checks_and_diff(
        self, run: RunRecord, call: ToolCall, result: ToolResult
    ) -> None:
        if run.plan is None:
            return
        if call.name in {"apply_patch", "create_file"} and result.ok:
            for check in run.plan.acceptance_checks:
                if check.command:
                    check.status = CheckStatus.PENDING
                    check.exit_code = None
                    check.evidence = "Files changed after the previous check."
            await self.broker.emit(
                run.id, EventType.DIFF_UPDATED, {"diff": self.workspace.diff(run.id)}
            )
        elif call.name == "run_command":
            argv = call.arguments.get("argv")
            for check in run.plan.acceptance_checks:
                if check.command == argv:
                    check.status = CheckStatus.PASSED if result.ok else CheckStatus.FAILED
                    check.exit_code = result.metadata.get("exit_code")
                    check.evidence = (result.output or result.error or "")[-1_000:]
        await self.broker.emit(
            run.id, EventType.PLAN_UPDATED, run.plan.model_dump(mode="json")
        )

    def _redact_result(self, result: ToolResult) -> ToolResult:
        result.output = self._redact(result.output)
        if result.error:
            result.error = self._redact(result.error)
        return result

    def _redact(self, text: str) -> str:
        redacted = text
        if self.settings.api_key:
            redacted = redacted.replace(self.settings.api_key, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", redacted)

    def _control(self, run_id: str) -> _Control:
        try:
            return self._controls[run_id]
        except KeyError as exc:
            raise InvalidRunAction(f"Run is not active: {run_id}") from exc

    def _clarification_round(self, run_id: str) -> int:
        return sum(
            event.type is EventType.CLARIFICATION_REQUESTED
            for event in self.storage.get_events(run_id)
        )

    @staticmethod
    def _missing_command_checks(plan: TaskPlan | None) -> list[str]:
        if plan is None:
            return ["approved plan"]
        return [
            check.label
            for check in plan.acceptance_checks
            if check.command and check.status is not CheckStatus.PASSED
        ]

    @staticmethod
    def _repair_incomplete_tool_protocol(run: RunRecord) -> None:
        if not run.messages:
            return
        last = run.messages[-1]
        if last.get("role") != "assistant" or not last.get("tool_calls"):
            return
        for call in last["tool_calls"]:
            run.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["function"]["name"],
                    "content": json.dumps(
                        {
                            "ok": False,
                            "error": "TraceForge stopped before this tool call completed.",
                        }
                    ),
                }
            )


def _model_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _questions_schema() -> dict[str, Any]:
    schema = ClarificationRequest.model_json_schema()
    schema.get("properties", {}).pop("round", None)
    schema["required"] = ["questions"]
    return schema


def _command_summary(call: ToolCall) -> str:
    argv = call.arguments.get("argv")
    if isinstance(argv, list):
        return "Run: " + " ".join(str(item) for item in argv)
    return f"Execute {call.name}"


def _rollback_payload(result: RollbackResult) -> dict[str, Any]:
    return {
        "restored": result.restored,
        "removed": result.removed,
        "conflicts": result.conflicts,
    }
