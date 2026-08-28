from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from traceforge.config import Settings
from traceforge.context import ContextManager
from traceforge.events import EventBroker
from traceforge.model_reasoning import ReasoningCapability, resolve_reasoning_capability
from traceforge.models import (
    ApprovalMode,
    ApprovalRequest,
    CheckStatus,
    ClarificationAnswer,
    ClarificationRequest,
    ConversationTurn,
    DirectResponse,
    EventType,
    InteractionMode,
    PlanGate,
    ReasoningEffort,
    RunEvent,
    RunRecord,
    RunState,
    TaskPlan,
    ToolCall,
    ToolResult,
    Verdict,
    VerificationReport,
    utc_now,
)
from traceforge.planning import assess_plan_gate
from traceforge.prompts import BUILDER_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, VERIFIER_SYSTEM_PROMPT
from traceforge.provider import ModelProvider, ModelResponse, ProviderError
from traceforge.storage import SecureCheckpointError, Storage
from traceforge.tools import PermissionDecision, PermissionResolution, ToolRegistry
from traceforge.workspace import RollbackResult, Workspace


class RunConflictError(RuntimeError):
    pass


class InvalidRunAction(RuntimeError):
    pass


class PlanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "revise"]
    feedback: str = Field(default="", max_length=2_000)


class PlanStepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    status: Literal["pending", "in_progress", "completed"]


class PlanUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: list[PlanStepUpdate] = Field(min_length=1, max_length=12)


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
        RunState.ANSWERED,
        RunState.EXECUTING,
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
    RunState.ANSWERED: {RunState.CREATED, RunState.ROLLED_BACK},
    RunState.SUCCEEDED: {RunState.CREATED, RunState.ROLLED_BACK},
    RunState.FAILED: {RunState.CREATED, RunState.ROLLED_BACK},
    RunState.CANCELLED: {RunState.CREATED, RunState.ROLLED_BACK},
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

    async def start_run(
        self,
        task: str,
        *,
        verifier_enabled: bool = True,
        project_id: str | None = None,
        mode: InteractionMode = InteractionMode.AGENT,
        approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> RunRecord:
        clean_task = task.strip()
        if not clean_task:
            raise ValueError("Task must not be empty")
        if self.storage.has_active_run(self.settings.workspace):
            raise RunConflictError("This workspace already has an active or interrupted run")
        self._reasoning_capability().validate(reasoning_effort)
        run = RunRecord(
            id=uuid4().hex,
            task=clean_task,
            workspace=str(self.settings.workspace),
            project_id=project_id,
            mode=mode,
            approval_mode=approval_mode,
            reasoning_effort=reasoning_effort,
            verifier_enabled=verifier_enabled,
            context_limit=self.settings.context_limit,
            turns=[
                ConversationTurn(
                    index=1,
                    request=clean_task,
                    mode=mode,
                    approval_mode=approval_mode,
                    reasoning_effort=reasoning_effort,
                )
            ],
        )
        self.storage.create_run(run)
        self._controls[run.id] = _Control()
        await self.broker.emit(
            run.id,
            EventType.STATE_CHANGED,
            {"state": run.state.value, "previous": None},
        )
        await self.broker.emit(
            run.id,
            EventType.TURN_STARTED,
            {
                "index": 1,
                "request": clean_task,
                "mode": mode.value,
                "approval_mode": approval_mode.value,
                "reasoning_effort": reasoning_effort.value,
            },
        )
        self._spawn(run.id, resume=False)
        return run

    async def follow_up(
        self,
        run_id: str,
        prompt: str,
        *,
        mode: InteractionMode = InteractionMode.AGENT,
        approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> RunRecord:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Follow-up prompt must not be empty")
        run = self.storage.get_run(run_id)
        if run.state not in {
            RunState.ANSWERED,
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            raise InvalidRunAction("Follow-up is available after the current turn stops")
        if self.storage.has_active_run(self.settings.workspace):
            raise RunConflictError("This workspace already has an active or interrupted run")
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise RunConflictError("Run is already active")
        self._reasoning_capability().validate(reasoning_effort)

        index = len(run.turns) + 1
        run.mode = mode
        run.approval_mode = approval_mode
        run.reasoning_effort = reasoning_effort
        run.turns.append(
            ConversationTurn(
                index=index,
                request=clean_prompt,
                mode=mode,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
            )
        )
        run.plan = None
        run.plan_gate = None
        run.plan_approved = False
        run.clarification = None
        run.pending_approval = None
        run.verification = None
        run.messages = []
        run.step_count = 0
        run.repair_cycles = 0
        run.context_limit = self.settings.context_limit
        run.error = None
        self._controls[run.id] = _Control()
        await self._transition(run, RunState.CREATED)
        await self.broker.emit(
            run.id,
            EventType.TURN_STARTED,
            {
                "index": index,
                "request": clean_prompt,
                "mode": mode.value,
                "approval_mode": approval_mode.value,
                "reasoning_effort": reasoning_effort.value,
            },
        )
        self._spawn(run.id, resume=False)
        return self.storage.get_run(run.id)

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

    async def decide_action(
        self, run_id: str, approval_id: str, *, approved: bool
    ) -> None:
        run = self.storage.get_run(run_id)
        if run.state is not RunState.AWAITING_ACTION_APPROVAL:
            raise InvalidRunAction("The run is not waiting for an action approval")
        if run.pending_approval is None or run.pending_approval.id != approval_id:
            raise InvalidRunAction("Approval is no longer pending")
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
            await self._abandon_pending_approval(run, cause="user_cancelled")
            if await self._transition(run, RunState.CANCELLED):
                await self._close_turn(run, "cancelled", "The user stopped this turn.")
        return self.storage.get_run(run_id)

    async def resume(self, run_id: str) -> RunRecord:
        run = self.storage.get_run(run_id)
        if run.state is not RunState.INTERRUPTED:
            raise InvalidRunAction("Only interrupted runs can be resumed")
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise RunConflictError("Run is already active")
        if run.provider_reasoning_cleanup_pending:
            try:
                self.storage.secure_checkpoint()
            except SecureCheckpointError as exc:
                raise InvalidRunAction(
                    "Provider-private cleanup is still waiting for an external SQLite reader. "
                    "Close external database readers before resuming."
                ) from exc
            run.provider_reasoning_cleanup_pending = False
            self.storage.save_run(run)
        effort = self._active_turn(run).reasoning_effort
        try:
            self._reasoning_capability().validate(effort)
        except ValueError as exc:
            raise InvalidRunAction(
                "The paused turn's reasoning effort is incompatible with the current exact "
                f"model route. Restore a compatible model setting before resuming. {exc}"
            ) from exc
        self._controls[run_id] = _Control()
        self._spawn(run_id, resume=True)
        return run

    async def rollback(self, run_id: str) -> RollbackResult:
        run = self.storage.get_run(run_id)
        if run.state is RunState.ANSWERED and not self.storage.list_snapshots(run_id):
            raise InvalidRunAction("Answer-only turns have no file changes to roll back")
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
                self.storage.save_run(run)
                await self.broker.emit(
                    run.id,
                    EventType.REPAIR_STARTED,
                    {
                        "cycle": run.repair_cycles,
                        "limit": self.settings.max_repair_cycles,
                        "verdict": report.verdict.value,
                        "summary": report.summary,
                        "findings": [
                            finding.model_dump(mode="json") for finding in report.findings
                        ],
                    },
                )
                await self._transition(run, RunState.EXECUTING)
        except asyncio.CancelledError:
            current = self.storage.get_run(run_id)
            if not current.state.terminal:
                await self._abandon_pending_approval(
                    current,
                    cause="process_shutdown" if self._shutting_down else "user_cancelled",
                )
                if self._shutting_down:
                    current.interrupted_from = current.state
                    await self._transition(current, RunState.INTERRUPTED)
                else:
                    if await self._transition(current, RunState.CANCELLED):
                        await self._close_turn(
                            current, "cancelled", "The user stopped this turn."
                        )
                        await self.broker.emit(
                            run_id,
                            EventType.RUN_COMPLETED,
                            {"state": RunState.CANCELLED.value},
                        )
            raise
        except ProviderError as exc:
            current = self.storage.get_run(run_id)
            if not current.state.terminal:
                message = self._redact(str(exc))
                if exc.retryable:
                    await self._interrupt_for_provider(current, message, exc.category)
                else:
                    await self._fail(current, message)
        except (ValidationError, ValueError, RuntimeError) as exc:
            current = self.storage.get_run(run_id)
            if not current.state.terminal:
                await self._fail(current, self._redact(str(exc)))
        except Exception as exc:
            current = self.storage.get_run(run_id)
            if not current.state.terminal:
                await self._fail(
                    current,
                    (
                        "TraceForge encountered an unexpected internal error "
                        f"({type(exc).__name__}). The run was stopped instead of being left active."
                    ),
                )
        finally:
            self._controls.pop(run_id, None)

    async def _prepare_resume(self, run: RunRecord) -> None:
        previous = run.interrupted_from
        await self._abandon_pending_approval(run, cause="process_restart")
        run.interrupted_from = None
        run.error = None
        repaired_calls = self._repair_incomplete_tool_protocol(run)
        if run.clarification is not None and not run.plan_approved:
            strategy = "await_clarification"
        elif run.plan is not None and not run.plan_approved:
            strategy = (
                "persisted_fast_path"
                if run.plan_gate
                and run.plan_gate.decision in {"auto_approved", "agent_continues"}
                else "await_plan_approval"
            )
        elif run.plan_approved:
            strategy = "inspect_before_execution"
        else:
            strategy = "restart_planning"
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.RUN_RESUMED,
            {
                "interrupted_from": previous.value if previous else None,
                "strategy": strategy,
                "incomplete_tool_calls_repaired": repaired_calls,
            },
        )
        if run.clarification is not None and not run.plan_approved:
            await self._transition(run, RunState.AWAITING_CLARIFICATION)
            answers = await self._await_clarification(run)
            self._append_clarification_answers(run, answers)
            run.clarification = None
            await self._transition(run, RunState.PLANNING)
        elif run.plan is not None and not run.plan_approved:
            if run.plan_gate and run.plan_gate.decision in {
                "auto_approved",
                "agent_continues",
            }:
                run.plan_approved = True
                run.messages = self._builder_messages(run, run.plan)
                await self._transition(run, RunState.EXECUTING)
            else:
                await self._transition(run, RunState.AWAITING_PLAN_APPROVAL)
                approved = await self._await_plan_decision(run)
                if approved:
                    run.plan_approved = True
                    run.messages = self._builder_messages(run, run.plan)
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
        request = self._current_request(run)
        if not run.messages or run.messages[0].get("content") != PLANNER_SYSTEM_PROMPT:
            run.messages = [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": self._conversation_context(run, request),
                },
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
                "respond_to_user",
                "Give a natural answer without claiming execution or verification.",
                DirectResponse.model_json_schema(),
            ),
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
            terminal_calls = [
                call
                for call in response.tool_calls
                if call.name in {"respond_to_user", "ask_questions", "submit_plan"}
            ]
            run.messages.append(self._assistant_message_for_storage(response))
            self.storage.save_run(run)
            if not response.tool_calls:
                non_tool_responses += 1
                if non_tool_responses >= 2:
                    raise RuntimeError(
                        "Planner did not submit a plan, clarification request, or direct response"
                    )
                run.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Use respond_to_user, ask_questions, or submit_plan now; do not "
                            "answer in prose only."
                        ),
                    }
                )
                continue
            if terminal_calls and (
                len(terminal_calls) != 1 or len(response.tool_calls) != 1
            ):
                error = (
                    "A terminal planning action must be called exactly once and alone. "
                    "Review any read results first, then choose respond_to_user, ask_questions, "
                    "or submit_plan in a new turn."
                )
                for call in response.tool_calls:
                    self._append_tool_error(run, call, error)
                run.messages.append({"role": "user", "content": error})
                self.storage.save_run(run)
                continue
            if response.content and all(
                call.name in {"list_files", "read_file", "search_text"}
                for call in response.tool_calls
            ):
                await self._emit_message(run, response.content, phase="planning")
            for call in response.tool_calls:
                if call.name in {"list_files", "read_file", "search_text"}:
                    result = await self.tools.execute(run.id, call)
                    self._append_tool_result(run, result)
                    await self._emit_tool_result(run, call, result)
                    continue
                if call.name == "respond_to_user":
                    try:
                        direct_response = DirectResponse.model_validate(call.arguments)
                    except ValidationError as exc:
                        await self._reject_invalid_planning_call(
                            run,
                            call,
                            label="direct response",
                            error=exc,
                        )
                        continue
                    content = self._redact(direct_response.content)
                    self._append_tool_result(
                        run,
                        ToolResult(
                            tool_call_id=call.id,
                            name=call.name,
                            ok=True,
                            output=(
                                "Response accepted. No file mutation, command execution, or "
                                "completion verification occurred."
                            ),
                        ),
                    )
                    self.storage.save_run(run)
                    await self._complete_answer(run, content)
                    return
                if call.name == "ask_questions":
                    round_number = self._clarification_round(run.id) + 1
                    if round_number > 2:
                        self._append_tool_error(
                            run,
                            call,
                            "At most two clarification rounds are allowed. Submit a justified "
                            "plan or use respond_to_user to explain the remaining blocker.",
                        )
                        continue
                    try:
                        clarification = ClarificationRequest(
                            questions=call.arguments.get("questions", []),
                            round=round_number,
                        )
                    except ValidationError as exc:
                        await self._reject_invalid_planning_call(
                            run,
                            call,
                            label="clarification request",
                            error=exc,
                        )
                        continue
                    run.clarification = clarification
                    self.storage.save_run(run)
                    await self._transition(run, RunState.AWAITING_CLARIFICATION)
                    answers = await self._await_clarification(run)
                    self._append_clarification_answers(run, answers, call.id)
                    run.clarification = None
                    await self._transition(run, RunState.PLANNING)
                    continue
                if call.name == "submit_plan":
                    try:
                        plan = TaskPlan.model_validate(call.arguments)
                    except ValidationError as exc:
                        await self._reject_invalid_planning_call(
                            run,
                            call,
                            label="plan",
                            error=exc,
                        )
                        continue
                    run.plan = plan
                    assessed_gate = assess_plan_gate(
                        request,
                        plan,
                        clarification_rounds=self._clarification_round(run.id),
                    )
                    if run.mode is InteractionMode.AGENT:
                        run.plan_gate = PlanGate(
                            decision="agent_continues",
                            risk=assessed_gate.risk,
                            reasons=[
                                "Agent mode continues without a plan approval pause",
                                *assessed_gate.reasons,
                            ],
                        )
                    else:
                        run.plan_gate = PlanGate(
                            decision="approval_required",
                            risk=assessed_gate.risk,
                            reasons=[
                                "Plan mode pauses for review before implementation",
                                *assessed_gate.reasons,
                            ],
                        )
                    self.storage.save_run(run)
                    await self.broker.emit(
                        run.id,
                        EventType.PLAN_GATED,
                        run.plan_gate.model_dump(mode="json"),
                    )
                    if run.mode is InteractionMode.AGENT:
                        await self.broker.emit(
                            run.id, EventType.PLAN_UPDATED, plan.model_dump(mode="json")
                        )
                        run.plan_approved = True
                        run.messages = self._builder_messages(run, plan)
                        await self._transition(run, RunState.EXECUTING)
                        return
                    await self._transition(run, RunState.AWAITING_PLAN_APPROVAL)
                    approved = await self._await_plan_decision(run, tool_call_id=call.id)
                    if approved:
                        run.plan_approved = True
                        run.messages = self._builder_messages(run, plan)
                        await self._transition(run, RunState.EXECUTING)
                        return
            self.storage.save_run(run)
        raise RuntimeError("Planning exceeded the maximum number of model turns")

    async def _builder_phase(self, run: RunRecord) -> None:
        repeated_failures: dict[str, int] = {}
        no_tool_responses = 0
        builder_tools = [
            *self.tools.schemas,
            _model_tool(
                "update_plan",
                "Update the status of one or more approved plan steps.",
                PlanUpdateRequest.model_json_schema(),
            ),
        ]
        while run.step_count < self.settings.max_steps:
            response = await self._complete_model(run, builder_tools)
            run.messages.append(self._assistant_message_for_storage(response))
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
            publish_progress = bool(response.content) and not any(
                call.name == "finish" for call in response.tool_calls
            )
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
                    self._set_turn_summary(run, str(call.arguments.get("summary", "")).strip())
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
                    publish_progress = False
                    break
                await self.broker.emit(
                    run.id, EventType.TOOL_REQUESTED, call.model_dump(mode="json")
                )
                if call.name == "update_plan":
                    await self._transition(run, RunState.EXECUTING)
                    await self.broker.emit(
                        run.id, EventType.TOOL_STARTED, call.model_dump(mode="json")
                    )
                    result = self._apply_plan_update(run, call)
                else:
                    permission = self.tools.resolve_permission(
                        call, run.plan, run.approval_mode
                    )
                    sandbox_bypass = permission.sandbox_bypass_on_allow
                    if permission.decision is PermissionDecision.DENY:
                        result = ToolResult(
                            tool_call_id=call.id,
                            name=call.name,
                            ok=False,
                            error=permission.reason,
                            metadata={
                                "permission": permission.as_metadata(
                                    outcome="denied",
                                    sandbox_bypass=False,
                                )
                            },
                        )
                    else:
                        approved = True
                        if permission.decision is PermissionDecision.ASK:
                            approved = await self._await_action_approval(
                                run, call, permission
                            )
                        if not approved:
                            result = ToolResult(
                                tool_call_id=call.id,
                                name=call.name,
                                ok=False,
                                error="User rejected this action.",
                                metadata={
                                    "permission": permission.as_metadata(
                                        outcome="user_rejected",
                                        sandbox_bypass=False,
                                    )
                                },
                            )
                        else:
                            await self._transition(run, RunState.EXECUTING)
                            await self.broker.emit(
                                run.id,
                                EventType.TOOL_STARTED,
                                call.model_dump(mode="json"),
                            )
                            result = await self.tools.execute(
                                run.id, call, sandbox_bypass=sandbox_bypass
                            )
                            result.metadata["permission"] = permission.as_metadata(
                                outcome=(
                                    "user_approved"
                                    if permission.decision is PermissionDecision.ASK
                                    else "auto_allowed"
                                ),
                                sandbox_bypass=sandbox_bypass,
                            )
                result = self._redact_result(result)
                publish_progress = publish_progress and result.ok
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
            if publish_progress and response.content:
                await self._emit_message(run, response.content, phase="building")
        raise RuntimeError(f"Builder exceeded the {self.settings.max_steps}-step limit")

    def _builder_messages(
        self, run: RunRecord, plan: TaskPlan
    ) -> list[dict[str, Any]]:
        evidence = self._planning_evidence(run.messages)
        task_context = (
            f"Current request:\n{self._current_request(run)}\n\n"
            f"Plan:\n{plan.markdown}\n\nStructured contract:\n{plan.model_dump_json()}"
        )
        previous = self._previous_turns_context(run)
        if previous:
            task_context = previous + "\n\n" + task_context
        if evidence:
            task_context += (
                "\n\nPlanning inspection evidence (reuse this before reading the same files "
                "again):\n" + evidence
            )
        return [
            {"role": "system", "content": BUILDER_SYSTEM_PROMPT},
            {"role": "user", "content": task_context},
        ]

    def _planning_evidence(self, messages: list[dict[str, Any]]) -> str:
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        for message in messages:
            for raw_call in message.get("tool_calls", []):
                function = raw_call.get("function", {})
                name = str(function.get("name", ""))
                if name not in {"list_files", "read_file", "search_text"}:
                    continue
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                calls[str(raw_call.get("id", ""))] = (name, arguments)

        sections: list[str] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            call = calls.get(str(message.get("tool_call_id", "")))
            if call is None:
                continue
            try:
                result = ToolResult.model_validate_json(str(message.get("content", "{}")))
            except (ValidationError, ValueError):
                continue
            if not result.ok:
                continue
            name, arguments = call
            sections.append(
                f"### {name}({json.dumps(arguments, ensure_ascii=False, sort_keys=True)})\n"
                f"{result.output}"
            )

        if not sections:
            return ""
        limit = min(self.settings.context_limit * 2, self.settings.model_output_limit * 8)
        rendered = "\n\n".join(sections)
        if len(rendered) <= limit:
            return rendered
        return rendered[:limit] + "\n... planning evidence truncated"

    async def _verifier_phase(self, run: RunRecord) -> VerificationReport:
        if not run.verifier_enabled:
            return VerificationReport(
                verdict=Verdict.INCONCLUSIVE,
                summary="Independent verification was disabled; command checks passed.",
            )
        assert run.plan is not None
        evidence = {
            "task": self._current_request(run),
            "conversation": self._previous_turns_context(run),
            "plan": run.plan.model_dump(mode="json"),
            "diff": self.workspace.diff(run.id)[:60_000],
            "tool_events": [
                self._event_for_model(event)
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
            prepared, _ = self.context.prepare(messages, verifier_tools)
            response = await self._request_model(run, prepared, verifier_tools)
            messages.append(response.as_assistant_message())
            submit_calls = [
                call for call in response.tool_calls if call.name == "submit_verification"
            ]
            read_calls = [
                call for call in response.tool_calls if call.name != "submit_verification"
            ]
            if (
                response.content
                and not submit_calls
                and read_calls
                and all(
                    call.name in {"list_files", "read_file", "search_text"}
                    for call in read_calls
                )
            ):
                await self._emit_message(run, response.content, phase="verifying")
            if len(submit_calls) == 1 and not read_calls:
                try:
                    return VerificationReport.model_validate(submit_calls[0].arguments)
                except ValidationError as exc:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": submit_calls[0].id,
                            "name": "submit_verification",
                            "content": ToolResult(
                                tool_call_id=submit_calls[0].id,
                                name="submit_verification",
                                ok=False,
                                error=f"Invalid verification report: {exc}",
                            ).model_dump_json(),
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "Correct the report schema and submit one verdict.",
                        }
                    )
                    continue
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
            if submit_calls and (read_calls or len(submit_calls) > 1):
                for call in submit_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": ToolResult(
                                tool_call_id=call.id,
                                name=call.name,
                                ok=False,
                                error=(
                                    "Submit exactly one verdict in a separate turn after reads."
                                ),
                            ).model_dump_json(),
                        }
                    )
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
        if run.plan:
            for step in run.plan.steps:
                step.status = "completed"
        self.storage.save_run(run)
        if not await self._transition(run, RunState.SUCCEEDED):
            return
        await self._close_turn(
            run,
            "succeeded",
            self._active_turn(run).summary or report.summary,
        )
        await self.broker.emit(
            run.id,
            EventType.RUN_COMPLETED,
            {
                "state": RunState.SUCCEEDED.value,
                "verification": report.model_dump(mode="json"),
                "diff": self.workspace.diff(run.id),
            },
        )

    async def _complete_answer(self, run: RunRecord, content: str) -> None:
        if not await self._transition(run, RunState.ANSWERED):
            return
        await self._close_turn(run, "answered", content)
        await self.broker.emit(
            run.id,
            EventType.RUN_COMPLETED,
            {"state": RunState.ANSWERED.value},
        )

    async def _fail(self, run: RunRecord, error: str) -> None:
        run.error = error
        self.storage.save_run(run)
        await self.broker.emit(run.id, EventType.ERROR, {"message": error})
        if not await self._transition(run, RunState.FAILED):
            return
        await self._close_turn(run, "failed", error)
        await self.broker.emit(
            run.id,
            EventType.RUN_COMPLETED,
            {"state": RunState.FAILED.value, "error": error},
        )

    async def _interrupt_for_provider(
        self, run: RunRecord, error: str, category: str
    ) -> None:
        previous = run.state
        run.interrupted_from = previous
        run.error = (
            f"{error} The workspace and run history were preserved. "
            "Check the connection or model settings, then resume."
        )
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.ERROR,
            {
                "message": run.error,
                "cause": "model_unavailable",
                "category": category,
                "recoverable": True,
            },
        )
        await self._transition(run, RunState.INTERRUPTED)

    async def _request_model(
        self,
        run: RunRecord,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        attempts = self.settings.model_retry_attempts
        delay = self.settings.model_retry_delay
        capability = self._reasoning_capability()
        effort = self._active_turn(run).reasoning_effort
        capability.validate(effort)
        wire_effort = (
            None
            if effort is ReasoningEffort.AUTO
            or (
                capability.transport == "deepseek_chat"
                and effort is ReasoningEffort.NONE
            )
            else effort.value
        )
        for attempt in range(1, attempts + 1):
            try:
                await self.broker.emit(
                    run.id,
                    EventType.MODEL_REQUESTED,
                    {
                        "turn_index": self._active_turn(run).index,
                        "phase": run.state.value,
                        "attempt": attempt,
                        "model": self.settings.model,
                        "requested_effort": effort.value,
                        "wire_effort": wire_effort,
                        "omitted": wire_effort is None,
                        "thinking": (
                            "disabled"
                            if capability.transport == "deepseek_chat"
                            and effort is ReasoningEffort.NONE
                            else (
                                "enabled"
                                if capability.transport == "deepseek_chat"
                                and effort is not ReasoningEffort.AUTO
                                else "provider_default"
                            )
                        ),
                        "capability_source": capability.source,
                    },
                )
                if effort is ReasoningEffort.AUTO:
                    response = await self.provider.complete(messages, tools)
                else:
                    response = await self.provider.complete(
                        messages,
                        tools,
                        reasoning_effort=effort,
                    )
                if not isinstance(response, ModelResponse):
                    raise ProviderError(
                        "Model provider returned an invalid response object",
                        category="provider_contract",
                    )
                return response
            except ProviderError as exc:
                if not exc.retryable or attempt >= attempts:
                    raise
                await self.broker.emit(
                    run.id,
                    EventType.MODEL_RETRY,
                    {
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                        "max_attempts": attempts,
                        "category": exc.category,
                        "delay_seconds": delay,
                    },
                )
                await asyncio.sleep(delay)
                delay *= 2
            except Exception as exc:
                raise ProviderError(
                    f"Model provider failed unexpectedly ({type(exc).__name__})",
                    category="provider_contract",
                ) from exc
        raise AssertionError("Model retry loop ended unexpectedly")

    async def _complete_model(
        self, run: RunRecord, tools: list[dict[str, Any]]
    ) -> ModelResponse:
        prepared, compacted = self.context.prepare(run.messages, tools)
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
        return await self._request_model(run, prepared, tools)

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
        run.plan_gate = None
        await self._transition(run, RunState.PLANNING)
        return False

    async def _await_action_approval(
        self, run: RunRecord, call: ToolCall, permission: PermissionResolution
    ) -> bool:
        approval = ApprovalRequest(
            id=uuid4().hex,
            tool_call=call,
            summary=_command_summary(call),
            reason=permission.reason,
            risk=permission.risk,
            approval_mode=permission.mode,
            policy_decision=permission.policy_decision.value,
            sandbox_bypass_on_approve=permission.sandbox_bypass_on_allow,
        )
        run.pending_approval = approval
        self.storage.save_run(run)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._control(run.id).approval_future = future
        await self._transition(run, RunState.AWAITING_ACTION_APPROVAL)
        await self.broker.emit(
            run.id,
            EventType.APPROVAL_REQUESTED,
            {
                **approval.model_dump(mode="json"),
                "mode": permission.mode.value,
                "policy_decision": permission.policy_decision.value,
            },
        )
        approved = await future
        run.pending_approval = None
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.APPROVAL_RESOLVED,
            {
                "approval_id": approval.id,
                "approved": approved,
                "outcome": "approved" if approved else "rejected",
                "mode": permission.mode.value,
                "sandbox_bypass": (
                    permission.sandbox_bypass_on_allow if approved else False
                ),
            },
        )
        await self._transition(run, RunState.EXECUTING)
        return approved

    async def _abandon_pending_approval(self, run: RunRecord, *, cause: str) -> None:
        approval = run.pending_approval
        if approval is None:
            return
        run.pending_approval = None
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.APPROVAL_RESOLVED,
            {
                "approval_id": approval.id,
                "approved": False,
                "outcome": "abandoned",
                "cause": cause,
                "mode": run.approval_mode.value,
                "sandbox_bypass": False,
            },
        )

    async def _transition(self, run: RunRecord, new_state: RunState) -> bool:
        same_state = run.state is new_state
        if not same_state and new_state not in _ALLOWED_TRANSITIONS[run.state]:
            raise RuntimeError(f"Invalid run transition: {run.state.value} -> {new_state.value}")
        if new_state.terminal:
            # Persist and physically checkpoint the scrub while the row is still recoverable.
            # A crash or busy WAL can then leave an interrupted run, never a terminal row that
            # still contains provider-private replay state.
            scrubbed = self._scrub_provider_reasoning(run)
            run.provider_reasoning_cleanup_pending = (
                run.provider_reasoning_cleanup_pending or scrubbed
            )
            self.storage.save_run(run)
            if run.provider_reasoning_cleanup_pending:
                try:
                    self.storage.secure_checkpoint()
                except SecureCheckpointError:
                    await self._interrupt_for_reasoning_cleanup(run, new_state)
                    return False
                run.provider_reasoning_cleanup_pending = False
                self.storage.save_run(run)
        if same_state:
            return True
        previous = run.state
        run.state = new_state
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.STATE_CHANGED,
            {"state": new_state.value, "previous": previous.value},
        )
        return True

    async def _interrupt_for_reasoning_cleanup(
        self, run: RunRecord, intended_state: RunState
    ) -> None:
        previous = run.state
        run.interrupted_from = previous
        run.state = RunState.INTERRUPTED
        run.error = (
            "Provider-private replay state was removed from the active record, but SQLite "
            "WAL cleanup is waiting for an external reader. Close external database readers, "
            "then stop or resume this task."
        )
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.ERROR,
            {
                "message": run.error,
                "cause": "provider_reasoning_cleanup_pending",
                "category": "storage_cleanup",
                "recoverable": True,
                "intended_state": intended_state.value,
            },
        )
        await self.broker.emit(
            run.id,
            EventType.STATE_CHANGED,
            {
                "state": RunState.INTERRUPTED.value,
                "previous": previous.value,
                "cause": "provider_reasoning_cleanup_pending",
            },
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
        model_result = result.model_copy(deep=True)
        model_result.output = _limit_for_model(
            model_result.output, self.settings.model_output_limit
        )
        if model_result.error:
            model_result.error = _limit_for_model(
                model_result.error, self.settings.model_output_limit
            )
        run.messages.append(
            {
                "role": "tool",
                "tool_call_id": model_result.tool_call_id,
                "name": model_result.name,
                "content": model_result.model_dump_json(),
            }
        )

    def _assistant_message_for_storage(self, response: ModelResponse) -> dict[str, Any]:
        message = response.as_assistant_message()
        private_reasoning = message.pop("reasoning_content", None)
        serialized = json.dumps(message, ensure_ascii=False)
        stored = cast(dict[str, Any], json.loads(self._redact(serialized)))
        if private_reasoning is not None:
            if self._redact(private_reasoning) != private_reasoning:
                raise ProviderError(
                    "Provider-private replay state could not be stored safely",
                    category="provider_contract",
                )
            stored["reasoning_content"] = private_reasoning
        return stored

    def _event_for_model(self, event: RunEvent) -> dict[str, Any]:
        payload = event.model_dump(mode="json")
        result = payload.get("payload", {}).get("result")
        if isinstance(result, dict):
            for field in ("output", "error"):
                value = result.get(field)
                if isinstance(value, str):
                    result[field] = _limit_for_model(
                        value, self.settings.model_output_limit
                    )
        return payload

    def _append_tool_error(self, run: RunRecord, call: ToolCall, error: str) -> None:
        self._append_tool_result(
            run,
            ToolResult(tool_call_id=call.id, name=call.name, ok=False, error=error),
        )

    async def _reject_invalid_planning_call(
        self,
        run: RunRecord,
        call: ToolCall,
        *,
        label: str,
        error: ValidationError,
    ) -> None:
        details = json.dumps(
            error.errors(include_url=False, include_input=False), ensure_ascii=False
        )
        result = ToolResult(
            tool_call_id=call.id,
            name=call.name,
            ok=False,
            error=f"Invalid {label} schema: {details}",
        )
        self._append_tool_result(run, result)
        run.messages.append(
            {
                "role": "user",
                "content": (
                    f"Correct the {label} to match the supplied tool schema, then call "
                    f"{call.name} again. Do not continue in prose."
                ),
            }
        )
        self.storage.save_run(run)
        await self._emit_tool_result(run, call, result)

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
        emit_diff = False
        changed_files: list[str] = []
        raw_changed_files = result.metadata.get("changed_files")
        if isinstance(raw_changed_files, list) and all(
            isinstance(path, str) for path in raw_changed_files
        ):
            changed_files = raw_changed_files
            turn_changed_files = self._active_turn(run).changed_files
            turn_changed_files.extend(changed_files)
            self._active_turn(run).changed_files = sorted(set(turn_changed_files))
        if run.plan is None:
            self.storage.save_run(run)
            return
        if call.name in {"apply_patch", "create_file"} and changed_files:
            for check in run.plan.acceptance_checks:
                if check.command:
                    check.status = CheckStatus.PENDING
                    check.exit_code = None
                    check.evidence = "Files changed after the previous check."
            emit_diff = True
        elif call.name == "run_command":
            argv = call.arguments.get("argv")
            for check in run.plan.acceptance_checks:
                if check.command == argv:
                    check.status = CheckStatus.PASSED if result.ok else CheckStatus.FAILED
                    check.exit_code = result.metadata.get("exit_code")
                    check.evidence = (result.output or result.error or "")[-1_000:]
        self.storage.save_run(run)
        if emit_diff:
            await self.broker.emit(
                run.id, EventType.DIFF_UPDATED, {"diff": self.workspace.diff(run.id)}
            )
        await self.broker.emit(
            run.id, EventType.PLAN_UPDATED, run.plan.model_dump(mode="json")
        )

    @staticmethod
    def _apply_plan_update(run: RunRecord, call: ToolCall) -> ToolResult:
        if run.plan is None:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error="No approved plan is available.",
            )
        try:
            request = PlanUpdateRequest.model_validate(call.arguments)
            by_id = {step.id: step for step in run.plan.steps}
            unknown = [update.id for update in request.updates if update.id not in by_id]
            if unknown:
                raise ValueError("Unknown plan step ids: " + ", ".join(unknown))
            for update in request.updates:
                by_id[update.id].status = update.status
        except (ValidationError, ValueError) as exc:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error=str(exc),
            )
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            ok=True,
            output="Updated: "
            + ", ".join(f"{update.id}={update.status}" for update in request.updates),
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

    @staticmethod
    def _active_turn(run: RunRecord) -> ConversationTurn:
        if run.turns:
            return run.turns[-1]
        turn = ConversationTurn(
            index=1,
            request=run.task,
            mode=run.mode,
            approval_mode=run.approval_mode,
            reasoning_effort=run.reasoning_effort,
        )
        run.turns.append(turn)
        return turn

    def _current_request(self, run: RunRecord) -> str:
        return self._active_turn(run).request

    @staticmethod
    def _previous_turns_context(run: RunRecord) -> str:
        completed = [turn for turn in run.turns[:-1] if turn.outcome != "in_progress"]
        if not completed:
            return ""
        lines = ["Earlier turns in this same task:"]
        for turn in completed[-6:]:
            lines.append(
                f"- Turn {turn.index}: {turn.request}\n"
                f"  Outcome: {turn.outcome}. Summary: {turn.summary or '(no summary)'}"
            )
        return "\n".join(lines)

    def _conversation_context(self, run: RunRecord, request: str) -> str:
        previous = self._previous_turns_context(run)
        current = f"Current request:\n{request}"
        return f"{previous}\n\n{current}" if previous else current

    def _set_turn_summary(self, run: RunRecord, summary: str) -> None:
        if summary:
            self._active_turn(run).summary = self._redact(summary)[:4_000]
            self.storage.save_run(run)

    async def _close_turn(
        self,
        run: RunRecord,
        outcome: Literal["answered", "succeeded", "failed", "cancelled"],
        summary: str,
    ) -> None:
        turn = self._active_turn(run)
        if turn.outcome != "in_progress":
            return
        turn.outcome = outcome
        limit = 20_000 if outcome == "answered" else 4_000
        turn.summary = self._redact(summary)[:limit]
        turn.completed_at = utc_now()
        self._scrub_provider_reasoning(run)
        self.storage.save_run(run)
        await self.broker.emit(
            run.id,
            EventType.TURN_COMPLETED,
            {
                "index": turn.index,
                "outcome": outcome,
                "summary": turn.summary,
                "changed_files": turn.changed_files,
                "approval_mode": turn.approval_mode.value,
                "reasoning_effort": turn.reasoning_effort.value,
            },
        )

    def _reasoning_capability(self) -> ReasoningCapability:
        return resolve_reasoning_capability(
            self.settings.model, base_url=self.settings.base_url
        )

    @staticmethod
    def _scrub_provider_reasoning(run: RunRecord) -> bool:
        scrubbed = False
        for message in run.messages:
            if "reasoning_content" in message:
                message.pop("reasoning_content")
                scrubbed = True
        return scrubbed

    def _clarification_round(self, run_id: str) -> int:
        count = 0
        for event in reversed(self.storage.get_events(run_id)):
            if event.type is EventType.TURN_STARTED:
                break
            if event.type is EventType.CLARIFICATION_REQUESTED:
                count += 1
        return count

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
    def _repair_incomplete_tool_protocol(run: RunRecord) -> int:
        if not run.messages:
            return 0
        last = run.messages[-1]
        if last.get("role") != "assistant" or not last.get("tool_calls"):
            return 0
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
        return len(last["tool_calls"])


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


def _limit_for_model(output: str, limit: int) -> str:
    if len(output.encode()) <= limit:
        return output
    half = limit // 2
    return f"{output[:half]}\n... output truncated for model ...\n{output[-half:]}"
