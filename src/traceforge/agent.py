from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from traceforge.config import Settings
from traceforge.context import ContextManager
from traceforge.credentials import validate_provider_credential
from traceforge.events import EventBroker
from traceforge.instructions import (
    WorkspaceInstructionLoader,
    render_workspace_instruction_context,
)
from traceforge.model_reasoning import ReasoningCapability, resolve_reasoning_capability
from traceforge.models import (
    ApprovalMode,
    ApprovalRequest,
    CheckStatus,
    ClarificationAnswer,
    ClarificationRequest,
    ConversationTurn,
    DecisionKind,
    DecisionRequest,
    DecisionStatus,
    DirectResponse,
    EventType,
    FinishRequest,
    InteractionMode,
    PlanGate,
    ProofPack,
    ReasoningEffort,
    RunEvent,
    RunRecord,
    RunState,
    TaskPlan,
    ToolCall,
    ToolResult,
    Verdict,
    VerificationReport,
    WorkspaceInstructionSnapshot,
    utc_now,
)
from traceforge.planning import assess_plan_gate
from traceforge.prompts import (
    BUILDER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
)
from traceforge.proof import (
    ProofNotReadyError,
    build_success_proof_pack,
    freeze_success_proof_pack,
)
from traceforge.provider import (
    ModelProvider,
    ModelResponse,
    ModelStreamDelta,
    ProviderError,
    StreamingModelProvider,
    close_model_provider,
)
from traceforge.storage import (
    DecisionConflictError,
    SecureCheckpointError,
    Storage,
    decision_payload_sha256,
)
from traceforge.streaming import (
    StableStreamingRedactor,
    boundary_safe_json_dumps,
    contains_compact_serialized_json_secret,
    contains_redactable_json_secret,
    contains_redactable_secret,
    contains_redactable_serialized_json_secret,
    json_string_field_prefix,
    redact_json_value,
    redact_text,
)
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
    decision_request_id: str | None = None
    decision_kind: DecisionKind | None = None


@dataclass(slots=True)
class _StreamTarget:
    tool_name: str
    field_name: str


class _AssistantOutputStream:
    _BATCH_CHARACTERS = 64
    _PARSE_CHARACTERS = 32
    _MAX_ARGUMENT_CHARACTERS = 300_000
    _MAX_NAME_CHARACTERS = 2_000
    _MAX_VISIBLE_CHARACTERS: ClassVar[dict[str, int]] = {
        "respond_to_user": 20_000,
        "finish": 4_000,
    }

    def __init__(
        self,
        broker: EventBroker,
        *,
        run_id: str,
        turn_index: int,
        phase: str,
        attempt: int,
        target: _StreamTarget,
        api_key: str,
    ) -> None:
        self._broker = broker
        self._run_id = run_id
        self._turn_index = turn_index
        self._phase = phase
        self._attempt = attempt
        self._target = target
        self._stream_id = uuid4().hex
        self._redactor = StableStreamingRedactor(api_key=api_key)
        self._names: dict[int, str] = {}
        self._argument_parts: dict[int, list[str]] = {}
        self._argument_lengths: dict[int, int] = {}
        self._target_index: int | None = None
        self._last_parsed_length = 0
        self._decoded = ""
        self._stable = ""
        self._published = ""
        self._segment_index = 0
        self._started = False
        self._completed = False
        self._accepted = False
        self._invalid = False

    async def on_delta(self, delta: ModelStreamDelta) -> None:
        if self._completed or self._invalid:
            return
        for call in delta.tool_calls:
            name = _append_stream_fragment(self._names.get(call.index, ""), call.name)
            argument_length = self._argument_lengths.get(call.index, 0) + len(call.arguments)
            if (
                len(name) > self._MAX_NAME_CHARACTERS
                or argument_length > self._MAX_ARGUMENT_CHARACTERS
            ):
                self._invalid = True
                await self.abort(status="discarded", reason="visible_output_size_limit")
                return
            self._names[call.index] = name
            if call.arguments:
                self._argument_parts.setdefault(call.index, []).append(call.arguments)
            self._argument_lengths[call.index] = argument_length
            if self._target_index is None and name == self._target.tool_name:
                self._target_index = call.index
            if (
                call.index == self._target_index
                and argument_length - self._last_parsed_length
                >= self._parse_interval(argument_length)
            ):
                self._last_parsed_length = argument_length
                arguments = "".join(self._argument_parts.get(call.index, []))
                await self._consume_arguments(arguments)

    async def resolve(self, response: ModelResponse) -> str | None:
        if self._completed:
            return self._stream_id if self._accepted else None
        if self._invalid:
            await self.abort(status="discarded", reason="invalid_streamed_output")
            return None
        if len(response.tool_calls) != 1 or response.tool_calls[0].name != self._target.tool_name:
            await self.abort(status="discarded", reason="response_structure_changed")
            return None
        call = response.tool_calls[0]
        if self._target_index is not None:
            arguments = "".join(self._argument_parts.get(self._target_index, []))
            prefix = json_string_field_prefix(arguments, self._target.field_name)
            raw_content = call.arguments.get(self._target.field_name)
            if not prefix.valid or prefix.value != raw_content or not prefix.complete:
                await self.abort(status="discarded", reason="stream_content_mismatch")
                return None
            maximum = self._MAX_VISIBLE_CHARACTERS[self._target.tool_name]
            if prefix.value is not None and len(prefix.value) > maximum:
                self._invalid = True
                await self.abort(status="discarded", reason="visible_output_size_limit")
                return None
        try:
            if self._target.tool_name == "respond_to_user":
                content = DirectResponse.model_validate(call.arguments).content
            elif self._target.tool_name == "finish":
                content = FinishRequest.model_validate(call.arguments).summary
            else:
                raise ValueError("Unsupported public output target")
        except (ValidationError, ValueError):
            await self.abort(status="discarded", reason="invalid_visible_output")
            return None
        try:
            self._stable = self._redactor.finish(content)
        except ValueError:
            await self.abort(status="discarded", reason="stream_redaction_mismatch")
            return None
        await self._publish_available(force=True)
        await self._broker.emit(
            self._run_id,
            EventType.ASSISTANT_OUTPUT_COMPLETED,
            {
                **self._base_payload(),
                "content": self._stable,
                "character_count": len(self._stable),
                "sha256": hashlib.sha256(self._stable.encode()).hexdigest(),
                "status": "provider_completed",
            },
        )
        self._accepted = True
        self._completed = True
        return self._stream_id

    async def abort(self, *, status: str, reason: str) -> None:
        if not self._started or self._completed:
            return
        await self._broker.abort_open_assistant_outputs(
            self._run_id,
            status=status,
            reason=reason,
            stream_id=self._stream_id,
        )
        self._completed = True

    async def _consume_arguments(self, arguments: str) -> None:
        prefix = json_string_field_prefix(arguments, self._target.field_name)
        if not prefix.valid:
            self._invalid = True
            await self.abort(status="discarded", reason="invalid_streamed_json_string")
            return
        if prefix.value is None:
            return
        maximum = self._MAX_VISIBLE_CHARACTERS[self._target.tool_name]
        if len(prefix.value) > maximum:
            self._invalid = True
            await self.abort(status="discarded", reason="visible_output_size_limit")
            return
        if not prefix.value.startswith(self._decoded):
            self._invalid = True
            await self.abort(status="discarded", reason="non_monotonic_stream_content")
            return
        self._decoded = prefix.value
        try:
            visible = self._decoded.strip() if self._target.tool_name == "finish" else self._decoded
            self._stable = self._redactor.update(visible)
        except ValueError:
            self._invalid = True
            await self.abort(status="discarded", reason="stream_redaction_mismatch")
            return
        await self._publish_available(force=False)

    async def _publish_available(self, *, force: bool) -> None:
        delta = self._stable[len(self._published) :]
        if not delta:
            return
        if not force and len(delta) < self._BATCH_CHARACTERS and "\n" not in delta:
            return
        if not self._started:
            await self._broker.emit(
                self._run_id,
                EventType.ASSISTANT_OUTPUT_STARTED,
                {**self._base_payload(), "status": "streaming"},
            )
            self._started = True
        next_segment = self._segment_index + 1
        await self._broker.emit(
            self._run_id,
            EventType.ASSISTANT_OUTPUT_DELTA,
            {
                **self._base_payload(),
                "segment_index": next_segment,
                "delta": delta,
            },
        )
        self._segment_index = next_segment
        self._published = self._stable

    @classmethod
    def _parse_interval(cls, argument_length: int) -> int:
        if argument_length < 4_096:
            return cls._PARSE_CHARACTERS
        if argument_length < 32_768:
            return 256
        return 2_048

    def _base_payload(self) -> dict[str, Any]:
        return {
            "turn_index": self._turn_index,
            "stream_id": self._stream_id,
            "phase": self._phase,
            "attempt": self._attempt,
            "surface": "conversation",
            "provisional": True,
            "source_tool": self._target.tool_name,
        }


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
        RunState.AWAITING_ACTION_APPROVAL,
        RunState.EXECUTING,
        RunState.CANCELLED,
        RunState.FAILED,
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
        if settings.api_key:
            validate_provider_credential(settings.api_key)
        self.settings = settings
        self.storage = storage
        self.storage.register_credential_guard(settings.api_key)
        self.provider = provider
        self.workspace = Workspace(settings.workspace, storage)
        self.workspace_instructions = WorkspaceInstructionLoader(
            self.workspace.root,
            api_key=settings.api_key,
        )
        self.tools = ToolRegistry(self.workspace, settings)
        self.context = ContextManager(settings.context_limit)
        self.broker = broker or EventBroker(storage)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._controls: dict[str, _Control] = {}
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False
        self._provider_closed = False
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
        parent_run_id: str | None = None,
    ) -> RunRecord:
        clean_task = task.strip()
        if not clean_task:
            raise ValueError("Task must not be empty")
        self._reject_credential_input(clean_task, label="Task text", action="starting")
        if self.storage.has_active_run(self.settings.workspace):
            raise RunConflictError("This workspace already has an active or interrupted run")
        self._reasoning_capability().validate(reasoning_effort)
        instruction_snapshot = self.workspace_instructions.capture()
        self._validate_initial_model_context(clean_task, instruction_snapshot)
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
        turn_started_payload: dict[str, Any] = {
            "index": 1,
            "request": clean_task,
            "mode": mode.value,
            "approval_mode": approval_mode.value,
            "reasoning_effort": reasoning_effort.value,
        }
        if parent_run_id is not None:
            turn_started_payload["continued_from_run_id"] = parent_run_id
        events = [
            (
                EventType.STATE_CHANGED,
                {"state": run.state.value, "previous": None},
            ),
            (EventType.TURN_STARTED, turn_started_payload),
            *self._workspace_instruction_events(instruction_snapshot, turn_index=1),
        ]
        persisted = self.storage.create_run(
            run,
            parent_run_id=parent_run_id,
            instruction_snapshot=instruction_snapshot,
            initial_events=events,
        )
        self.tools.bind_workspace_instruction_snapshot(
            run.id,
            instruction_snapshot.snapshot_sha256,
        )
        self._controls[run.id] = _Control()
        await self._publish_persisted(persisted)
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
        async with self._lifecycle_lock(run_id):
            return await self._follow_up_locked(
                run_id,
                prompt,
                mode=mode,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
            )

    async def continue_after_rollback(
        self,
        run_id: str,
        prompt: str,
        *,
        mode: InteractionMode = InteractionMode.AGENT,
        approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> RunRecord:
        """Start a successor run with a fresh snapshot boundary after rollback."""

        async with self._lifecycle_lock(run_id):
            clean_prompt = prompt.strip()
            if not clean_prompt:
                raise ValueError("Follow-up prompt must not be empty")
            self._reject_credential_input(
                clean_prompt,
                label="Follow-up text",
                action="continuing",
            )
            parent = self.storage.get_run(run_id)
            if parent.state is not RunState.ROLLED_BACK:
                raise InvalidRunAction(
                    "A rollback successor is available only after rollback completes"
                )
            await self._wait_for_terminal_task(run_id)
            parent = self.storage.get_run(run_id)
            if parent.state is not RunState.ROLLED_BACK:
                raise InvalidRunAction(
                    "A rollback successor is available only after rollback completes"
                )
            if parent.workspace != str(self.settings.workspace):
                raise InvalidRunAction("The rolled-back task belongs to another workspace")
            self._require_safe_persisted_context(parent)
            successor_id = self.storage.get_successor_run_id(parent.id)
            if successor_id is not None:
                successor = self.storage.get_run(successor_id)
                first_turn = successor.turns[0] if successor.turns else None
                if (
                    successor.task == clean_prompt
                    and first_turn is not None
                    and first_turn.request == clean_prompt
                    and first_turn.mode is mode
                    and first_turn.approval_mode is approval_mode
                    and first_turn.reasoning_effort is reasoning_effort
                ):
                    return successor
                raise InvalidRunAction(f"This rolled-back task already continued as {successor.id}")
            return await self.start_run(
                clean_prompt,
                verifier_enabled=parent.verifier_enabled,
                project_id=parent.project_id,
                mode=mode,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
                parent_run_id=parent.id,
            )

    async def _follow_up_locked(
        self,
        run_id: str,
        prompt: str,
        *,
        mode: InteractionMode,
        approval_mode: ApprovalMode,
        reasoning_effort: ReasoningEffort,
    ) -> RunRecord:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Follow-up prompt must not be empty")
        self._reject_credential_input(
            clean_prompt,
            label="Follow-up text",
            action="continuing",
        )
        run = self.storage.get_run(run_id)
        if run.state not in {
            RunState.ANSWERED,
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            raise InvalidRunAction("Follow-up is available after the current turn stops")
        self._require_safe_persisted_context(run)
        await self._wait_for_terminal_task(run_id)
        run = self.storage.get_run(run_id)
        if run.provider_reasoning_cleanup_pending:
            try:
                self.storage.secure_checkpoint()
            except SecureCheckpointError as exc:
                raise InvalidRunAction(
                    "Credential-conflict cleanup is still waiting for an external SQLite "
                    "reader. Close external database readers before continuing this task."
                ) from exc
            self.storage.finish_credential_conflict_cleanup(run.id)
            run = self.storage.get_run(run_id)
        if run.state is RunState.SUCCEEDED:
            self._freeze_success_or_reject(run)
        if self.storage.has_active_run(self.settings.workspace):
            raise RunConflictError("This workspace already has an active or interrupted run")
        self._reasoning_capability().validate(reasoning_effort)
        instruction_snapshot = self.workspace_instructions.capture()
        self._validate_initial_model_context(clean_prompt, instruction_snapshot)

        index = max((turn.index for turn in run.turns), default=0) + 1
        previous_state = run.state
        self._validate_transition(run, RunState.CREATED)
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
        run.state = RunState.CREATED
        run.interrupted_from = None
        run.provider_reasoning_cleanup_pending = False
        persisted = self.storage.begin_turn(
            run,
            previous_state=previous_state,
            instruction_snapshot=instruction_snapshot,
            events=[
                (
                    EventType.STATE_CHANGED,
                    {
                        "state": RunState.CREATED.value,
                        "previous": previous_state.value,
                    },
                ),
                (
                    EventType.TURN_STARTED,
                    {
                        "index": index,
                        "request": clean_prompt,
                        "mode": mode.value,
                        "approval_mode": approval_mode.value,
                        "reasoning_effort": reasoning_effort.value,
                    },
                ),
                *self._workspace_instruction_events(
                    instruction_snapshot,
                    turn_index=index,
                ),
            ],
        )
        self.tools.bind_workspace_instruction_snapshot(
            run.id,
            instruction_snapshot.snapshot_sha256,
        )
        self._controls[run.id] = _Control()
        await self._publish_persisted(persisted)
        self._spawn(run.id, resume=False)
        return self.storage.get_run(run.id)

    async def answer_clarification(
        self,
        run_id: str,
        answers: list[ClarificationAnswer],
        *,
        request_id: str | None = None,
    ) -> None:
        self._reject_credential_input(
            [answer.model_dump(mode="json") for answer in answers],
            label="Clarification answer",
            action="submitting it",
        )
        request_id = request_id or self._active_decision_id(run_id, DecisionKind.CLARIFICATION)
        payload = {"answers": [answer.model_dump(mode="json") for answer in answers]}
        receipt = self._decision_or_reject(run_id, request_id)
        if receipt.status is DecisionStatus.PENDING:
            run = self.storage.get_run(run_id)
            if run.clarification is None:
                raise InvalidRunAction("The clarification request is no longer active")
            expected = {question.id: question for question in run.clarification.questions}
            supplied = {answer.question_id: answer for answer in answers}
            if len(supplied) != len(answers) or supplied.keys() != expected.keys():
                raise InvalidRunAction("Every clarification question must be answered exactly once")
            for question_id, answer in supplied.items():
                if answer.option_id and answer.option_id not in {
                    option.id for option in expected[question_id].options
                }:
                    raise InvalidRunAction(f"Unknown option for {question_id}: {answer.option_id}")
            self._require_decision_subject(receipt, run.clarification.model_dump(mode="json"))
        accepted = self._accept_decision_or_reject(
            run_id, request_id, DecisionKind.CLARIFICATION, payload
        )
        self._signal_decision(accepted)

    async def decide_plan(
        self,
        run_id: str,
        decision: PlanDecision,
        *,
        request_id: str | None = None,
    ) -> None:
        self._reject_credential_input(
            decision.model_dump(mode="json"),
            label="Plan decision",
            action="submitting it",
        )
        request_id = request_id or self._active_decision_id(run_id, DecisionKind.PLAN)
        receipt = self._decision_or_reject(run_id, request_id)
        if receipt.status is DecisionStatus.PENDING:
            run = self.storage.get_run(run_id)
            if run.plan is None:
                raise InvalidRunAction("The plan request is no longer active")
            self._require_decision_subject(receipt, run.plan.model_dump(mode="json"))
        accepted = self._accept_decision_or_reject(
            run_id,
            request_id,
            DecisionKind.PLAN,
            decision.model_dump(mode="json"),
        )
        self._signal_decision(accepted)

    async def decide_action(self, run_id: str, approval_id: str, *, approved: bool) -> None:
        try:
            receipt = self._decision_or_reject(run_id, approval_id)
        except InvalidRunAction as exc:
            raise InvalidRunAction("The action approval is no longer pending") from exc
        if receipt.status is DecisionStatus.PENDING:
            run = self.storage.get_run(run_id)
            if run.pending_approval is None or run.pending_approval.id != approval_id:
                raise InvalidRunAction("Approval is no longer pending")
            try:
                self._require_safe_action_approval(run.pending_approval)
            except ProviderError as exc:
                await self._abandon_active_decision(
                    run, cause="unsafe_persisted_action"
                )
                raise InvalidRunAction(
                    "Approval cannot be accepted because its persisted tool call is unsafe"
                ) from exc
            self._require_decision_subject(receipt, run.pending_approval.model_dump(mode="json"))
        accepted = self._accept_decision_or_reject(
            run_id,
            approval_id,
            DecisionKind.ACTION,
            {"approved": approved},
        )
        self._signal_decision(accepted)

    async def cancel(self, run_id: str) -> RunRecord:
        async with self._lifecycle_lock(run_id):
            return await self._cancel_locked(run_id)

    async def _cancel_locked(self, run_id: str) -> RunRecord:
        run = self.storage.get_run(run_id)
        if run.state.terminal:
            return run
        instruction_snapshot = self._stored_instruction_snapshot_for_recovery(run)
        if run.state is RunState.INTERRUPTED and self._persisted_context_is_unsafe(
            run,
            instruction_snapshot=instruction_snapshot,
        ):
            events = self.storage.commit_credential_conflict_cancellation(run_id)
            self.tools.clear_workspace_instruction_snapshot(run_id)
            await self._publish_persisted(events)
            try:
                self.storage.secure_checkpoint()
            except SecureCheckpointError:
                # The run is already terminal and no longer blocks its workspace. Startup will
                # retry the physical WAL cleanup while the durable marker remains set.
                pass
            else:
                self.storage.finish_credential_conflict_cleanup(run_id)
            return self.storage.get_run(run_id)
        await self.tools.cancel(run_id)
        run = self.storage.get_run(run_id)
        if run.state.terminal:
            return run
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            run = self.storage.get_run(run_id)
        if run.state.terminal:
            return run
        if run.state is RunState.INTERRUPTED:
            await self._abandon_open_output_streams(
                run,
                status="cancelled",
                reason="user_cancelled",
            )
            await self._abandon_active_decision(run, cause="user_cancelled")
            await self._commit_terminal_turn(
                run,
                RunState.CANCELLED,
                "cancelled",
                "The user stopped this turn.",
                completion_payload={"state": RunState.CANCELLED.value},
            )
        return self.storage.get_run(run_id)

    async def resume(self, run_id: str) -> RunRecord:
        async with self._lifecycle_lock(run_id):
            return self._resume_locked(run_id)

    def _resume_locked(self, run_id: str) -> RunRecord:
        run = self.storage.get_run(run_id)
        if run.state is not RunState.INTERRUPTED:
            raise InvalidRunAction("Only interrupted runs can be resumed")
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise RunConflictError("Run is already active")
        instruction_snapshot = self._workspace_instruction_snapshot(run)
        self._require_safe_persisted_context(
            run,
            instruction_snapshot=instruction_snapshot,
        )
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
        self.tools.bind_workspace_instruction_snapshot(
            run.id,
            instruction_snapshot.snapshot_sha256,
        )
        self._controls[run_id] = _Control()
        self._spawn(run_id, resume=True)
        return run

    async def rollback(self, run_id: str) -> RollbackResult:
        async with self._lifecycle_lock(run_id):
            return await self._rollback_locked(run_id)

    async def _rollback_locked(self, run_id: str) -> RollbackResult:
        run = self.storage.get_run(run_id)
        if run.state is RunState.ROLLED_BACK:
            persisted = self._persisted_rollback_result(run_id)
            if persisted is None:
                raise InvalidRunAction(
                    "Rollback already completed, but its persisted result is unavailable"
                )
            return persisted
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise RunConflictError("Run is already active")
        if run.state is RunState.ANSWERED and not self.storage.list_snapshots(run_id):
            raise InvalidRunAction("Answer-only turns have no file changes to roll back")
        if not run.state.terminal and run.state is not RunState.INTERRUPTED:
            raise InvalidRunAction("Cancel the active run before rolling it back")
        if run.state.terminal:
            await self._wait_for_terminal_task(run_id)
            run = self.storage.get_run(run_id)
        if run.state is RunState.SUCCEEDED:
            self._freeze_success_or_reject(run)
        await self._abandon_open_output_streams(
            run,
            status="discarded",
            reason="run_rolled_back",
        )
        await self._abandon_active_decision(run, cause="run_rolled_back")
        previous = run.state
        self._validate_transition(run, RunState.ROLLED_BACK)
        if not await self._prepare_terminal_cleanup(run, RunState.ROLLED_BACK):
            raise InvalidRunAction(
                "Rollback is waiting for secure history cleanup; close external SQLite "
                "readers and retry"
            )
        result = self.workspace.rollback(run_id)
        turn_payload = None
        if self._active_turn(run).outcome == "in_progress":
            turn_payload = self._close_turn_in_memory(
                run,
                "cancelled",
                "The interrupted turn was rolled back.",
            )
        run.state = RunState.ROLLED_BACK
        events = self.storage.commit_rollback(
            run,
            previous_state=previous,
            rollback_payload=_rollback_payload(result),
            turn_payload=turn_payload,
        )
        await self._publish_persisted(events)
        return result

    def _persisted_rollback_result(self, run_id: str) -> RollbackResult | None:
        event = next(
            (
                candidate
                for candidate in reversed(self.storage.get_events(run_id))
                if candidate.type is EventType.ROLLBACK_COMPLETED
            ),
            None,
        )
        if event is None:
            return None

        def paths(key: str) -> list[str]:
            values = event.payload.get(key)
            return (
                [value for value in values if isinstance(value, str)]
                if isinstance(values, list)
                else []
            )

        return RollbackResult(
            restored=paths("restored"),
            removed=paths("removed"),
            conflicts=paths("conflicts"),
        )

    async def get_proof_pack(
        self, run_id: str, turn_index: int | None = None
    ) -> tuple[RunRecord, ProofPack | None]:
        async with self._lifecycle_lock(run_id):
            run = self.storage.get_run(run_id)
            if run.state is RunState.SUCCEEDED:
                await self._wait_for_terminal_task(run_id)
                run = self.storage.get_run(run_id)
                self._freeze_success_or_reject(run)
            return run, self.storage.get_proof_pack(run_id, turn_index)

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
        if not self._provider_closed:
            await close_model_provider(self.provider)
            self._provider_closed = True

    def _spawn(self, run_id: str, *, resume: bool) -> None:
        task = asyncio.create_task(self._run(run_id, resume=resume), name=f"traceforge:{run_id}")
        self._tasks[run_id] = task

    def _lifecycle_lock(self, run_id: str) -> asyncio.Lock:
        return self._lifecycle_locks.setdefault(run_id, asyncio.Lock())

    async def _wait_for_terminal_task(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))

    def _freeze_success_or_reject(self, run: RunRecord) -> ProofPack:
        try:
            return freeze_success_proof_pack(run, self.storage)
        except ProofNotReadyError as exc:
            raise InvalidRunAction(str(exc)) from exc

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
                if run.step_count >= self.settings.max_steps:
                    await self._fail(
                        run,
                        "Independent verification did not pass, and the Builder exhausted the "
                        "total non-terminal tool action budget before a repair could start.",
                    )
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
                            "findings and rerun acceptance checks:\n"
                            f"{boundary_safe_json_dumps(report.model_dump(mode='json'))}"
                        ),
                    }
                )
                self.storage.save_run(run)
                await self._abandon_open_output_streams(
                    run,
                    status="discarded",
                    reason="verification_rejected",
                )
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
                if self._shutting_down:
                    await self._transition(
                        current,
                        RunState.INTERRUPTED,
                        interruption_reason="process_shutdown",
                    )
                else:
                    self._mark_uncertain_started_approvals(run_id)
                    await self._abandon_open_output_streams(
                        current,
                        status="cancelled",
                        reason="user_cancelled",
                    )
                    await self._abandon_active_decision(current, cause="user_cancelled")
                    await self._commit_terminal_turn(
                        current,
                        RunState.CANCELLED,
                        "cancelled",
                        "The user stopped this turn.",
                        completion_payload={"state": RunState.CANCELLED.value},
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
        instruction_snapshot = self._workspace_instruction_snapshot(run)
        self.tools.bind_workspace_instruction_snapshot(
            run.id,
            instruction_snapshot.snapshot_sha256,
        )
        previous = run.interrupted_from
        if run.pending_approval is not None:
            try:
                self._require_safe_action_approval(run.pending_approval)
            except ProviderError:
                await self._abandon_active_decision(
                    run, cause="unsafe_persisted_action"
                )
                raise
        await self._abandon_open_output_streams(
            run,
            status="discarded",
            reason="run_resumed",
        )
        active_decision = self.storage.get_active_decision(run.id)
        if run.pending_approval is not None and (
            active_decision is None or active_decision.kind is not DecisionKind.ACTION
        ):
            await self._abandon_pending_approval(run, cause="process_restart")
        run.interrupted_from = None
        run.error = None
        preserved_decision = active_decision is not None and (
            (active_decision.kind is DecisionKind.CLARIFICATION and run.clarification is not None)
            or (active_decision.kind is DecisionKind.PLAN and run.plan is not None)
            or (active_decision.kind is DecisionKind.ACTION and run.pending_approval is not None)
        )
        if active_decision is not None and not preserved_decision:
            await self._abandon_active_decision(run, cause="resume_subject_mismatch")
            active_decision = None
        uncertain_approvals = self._mark_uncertain_started_approvals(run.id)
        repaired_calls = 0 if preserved_decision else self._repair_incomplete_tool_protocol(run)
        if run.clarification is not None and not run.plan_approved:
            strategy = (
                "consume_accepted_clarification"
                if active_decision
                and active_decision.kind is DecisionKind.CLARIFICATION
                and active_decision.status is DecisionStatus.ACCEPTED
                else "await_clarification"
            )
        elif run.plan is not None and not run.plan_approved:
            strategy = (
                "persisted_fast_path"
                if run.plan_gate and run.plan_gate.decision in {"auto_approved", "agent_continues"}
                else (
                    "consume_accepted_plan"
                    if active_decision
                    and active_decision.kind is DecisionKind.PLAN
                    and active_decision.status is DecisionStatus.ACCEPTED
                    else "await_plan_approval"
                )
            )
        elif run.pending_approval is not None and active_decision is not None:
            strategy = (
                "consume_accepted_action"
                if active_decision.status is DecisionStatus.ACCEPTED
                else "await_action_approval"
            )
        elif run.plan_approved:
            strategy = "inspect_before_execution"
        else:
            strategy = "restart_planning"
        self.storage.save_run(run)
        resumed_payload: dict[str, Any] = {
            "interrupted_from": previous.value if previous else None,
            "strategy": strategy,
            "incomplete_tool_calls_repaired": repaired_calls,
        }
        if uncertain_approvals:
            resumed_payload["uncertain_action_approvals"] = uncertain_approvals
        await self.broker.emit(
            run.id,
            EventType.RUN_RESUMED,
            resumed_payload,
        )
        if run.clarification is not None and not run.plan_approved:
            source_tool_call_id = self._pending_tool_call_id(run, "ask_questions")
            existing = (
                active_decision
                if active_decision and active_decision.kind is DecisionKind.CLARIFICATION
                else None
            )
            request_id, answers = await self._await_clarification(run, existing=existing)
            await self._apply_clarification_decision(
                run,
                request_id,
                answers,
                tool_call_id=source_tool_call_id,
            )
        elif run.plan is not None and not run.plan_approved:
            if run.plan_gate and run.plan_gate.decision in {
                "auto_approved",
                "agent_continues",
            }:
                run.plan_approved = True
                run.messages = self._builder_messages(run, run.plan)
                await self._transition(run, RunState.EXECUTING)
            else:
                source_tool_call_id = self._pending_tool_call_id(run, "submit_plan")
                existing = (
                    active_decision
                    if active_decision and active_decision.kind is DecisionKind.PLAN
                    else None
                )
                request_id, decision = await self._await_plan_decision(run, existing=existing)
                await self._apply_plan_decision(
                    run,
                    request_id,
                    decision,
                    tool_call_id=source_tool_call_id,
                )
        elif run.pending_approval is not None and active_decision is not None:
            await self._resume_action_decision(run, active_decision)
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
        planning_tools = self._planning_tools()
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
            if terminal_calls and (len(terminal_calls) != 1 or len(response.tool_calls) != 1):
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
                    result = self._redact_result(await self.tools.execute(run.id, call))
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
                    await self._complete_answer(
                        run,
                        content,
                        final_stream_id=response.output_stream_id,
                    )
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
                    request_id, answers = await self._await_clarification(run)
                    await self._apply_clarification_decision(
                        run, request_id, answers, tool_call_id=call.id
                    )
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
                    if run.mode is InteractionMode.PLAN:
                        run.plan_gate = PlanGate(
                            decision="approval_required",
                            risk=assessed_gate.risk,
                            reasons=[
                                "Plan mode pauses for review before implementation",
                                *assessed_gate.reasons,
                            ],
                        )
                    else:
                        run.plan_gate = assessed_gate
                    self.storage.save_run(run)
                    await self.broker.emit(
                        run.id,
                        EventType.PLAN_GATED,
                        run.plan_gate.model_dump(mode="json"),
                    )
                    if run.plan_gate.decision == "auto_approved":
                        await self.broker.emit(
                            run.id, EventType.PLAN_UPDATED, plan.model_dump(mode="json")
                        )
                        run.plan_approved = True
                        run.messages = self._builder_messages(run, plan)
                        await self._transition(run, RunState.EXECUTING)
                        return
                    request_id, decision = await self._await_plan_decision(run)
                    approved = await self._apply_plan_decision(
                        run, request_id, decision, tool_call_id=call.id
                    )
                    if approved:
                        return
            self.storage.save_run(run)
        raise RuntimeError("Planning exceeded the maximum number of model turns")

    async def _builder_phase(self, run: RunRecord) -> None:
        repeated_failures: dict[str, int] = {}
        no_tool_responses = 0
        builder_tools = self._builder_tools()
        finish_tools = [
            schema for schema in builder_tools if schema["function"]["name"] == "finish"
        ]
        consecutive_rejected_batches = 0

        async def reject_batch(
            calls: list[ToolCall],
            *,
            error: str,
            correction: str,
            fatal_error: str | None = None,
        ) -> None:
            nonlocal consecutive_rejected_batches
            await self._reject_builder_batch(run, calls, error=error, correction=correction)
            if fatal_error is not None:
                raise RuntimeError(fatal_error)
            consecutive_rejected_batches += 1
            if consecutive_rejected_batches >= 3:
                raise RuntimeError("Builder returned three consecutive rejected tool-call batches")

        while True:
            action_budget_exhausted = run.step_count >= self.settings.max_steps
            response = await self._complete_model(
                run, finish_tools if action_budget_exhausted else builder_tools
            )
            run.messages.append(self._assistant_message_for_storage(response))
            if not response.tool_calls:
                no_tool_responses += 1
                if no_tool_responses >= 2:
                    raise RuntimeError("Builder stopped without calling finish")
                run.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The non-terminal tool action budget is exhausted. Call finish alone "
                            "with a concise summary."
                            if action_budget_exhausted
                            else "Continue with tools, or call finish alone with a concise summary."
                        ),
                    }
                )
                self.storage.save_run(run)
                continue
            no_tool_responses = 0
            finish_calls = [call for call in response.tool_calls if call.name == "finish"]
            if finish_calls and (len(finish_calls) != 1 or len(response.tool_calls) != 1):
                await reject_batch(
                    response.tool_calls,
                    error=(
                        "finish must be called exactly once and alone. No tool call in this "
                        "response was executed."
                    ),
                    correction=(
                        "Review the rejected batch, then call finish alone or issue only the "
                        "non-terminal tools still needed in a new turn."
                    ),
                )
                continue
            if finish_calls:
                call = finish_calls[0]
                try:
                    finish = FinishRequest.model_validate(call.arguments)
                except ValidationError as exc:
                    details = boundary_safe_json_dumps(
                        exc.errors(include_url=False, include_input=False)
                    )
                    await reject_batch(
                        [call],
                        error=f"Invalid finish schema: {details}",
                        correction=(
                            "Correct finish to match the supplied schema, then call it alone."
                        ),
                    )
                    continue
                missing = self._missing_command_checks(run.plan)
                if missing:
                    if response.output_stream_id:
                        await self._emit_output_abort(
                            run,
                            response.output_stream_id,
                            status="discarded",
                            reason="completion_checks_missing",
                        )
                    await reject_batch(
                        [call],
                        error=("Command checks need fresh passing evidence: " + ", ".join(missing)),
                        correction=(
                            "Run every missing approved command check before calling finish alone."
                        ),
                        fatal_error=(
                            "Builder exhausted the non-terminal tool action budget before all "
                            "command checks had fresh passing evidence"
                            if action_budget_exhausted
                            else None
                        ),
                    )
                    continue
                self._set_turn_summary(
                    run,
                    finish.summary,
                    stream_id=response.output_stream_id,
                )
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

            remaining_actions = max(0, self.settings.max_steps - run.step_count)
            if len(response.tool_calls) > remaining_actions:
                await reject_batch(
                    response.tool_calls,
                    error=(
                        "This batch would exceed the non-terminal tool action budget "
                        f"({remaining_actions} remaining, {len(response.tool_calls)} requested). "
                        "No tool call in this response was executed."
                    ),
                    correction=(
                        "Submit a smaller non-terminal tool batch within the remaining budget, "
                        "or call finish alone with the current result."
                    ),
                )
                continue

            publish_progress = bool(response.content)
            batch_succeeded = True
            recovery_messages: list[dict[str, str]] = []
            for call in response.tool_calls:
                run.step_count += 1
                persisted_action_result: ToolResult | None = None
                approval_request_id: str | None = None
                await self.broker.emit(
                    run.id, EventType.TOOL_REQUESTED, self._public_tool_call_payload(call)
                )
                if call.name == "update_plan":
                    await self._transition(run, RunState.EXECUTING)
                    await self.broker.emit(
                        run.id, EventType.TOOL_STARTED, self._public_tool_call_payload(call)
                    )
                    result = self._apply_plan_update(run, call)
                else:
                    permission = self.tools.resolve_permission(call, run.plan, run.approval_mode)
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
                            (
                                approved,
                                approval_request_id,
                                persisted_action_result,
                            ) = await self._await_action_approval(run, call, permission)
                        if not approved:
                            if persisted_action_result is None:
                                raise RuntimeError(
                                    "Rejected action is missing its durable tool result"
                                )
                            result = persisted_action_result
                        else:
                            await self._transition(run, RunState.EXECUTING)
                            if approval_request_id is None:
                                await self.broker.emit(
                                    run.id,
                                    EventType.TOOL_STARTED,
                                    self._public_tool_call_payload(call),
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
                batch_succeeded = batch_succeeded and result.ok
                if persisted_action_result is None:
                    self._append_tool_result(run, result)
                await self._update_checks_and_diff(run, call, result)
                if persisted_action_result is None:
                    await self._emit_tool_result(
                        run,
                        call,
                        result,
                        approval_request_id=approval_request_id,
                    )
                if not result.ok:
                    fingerprint = boundary_safe_json_dumps(
                        {"name": call.name, "arguments": call.arguments}, sort_keys=True
                    )
                    repeated_failures[fingerprint] = repeated_failures.get(fingerprint, 0) + 1
                    if repeated_failures[fingerprint] == 2:
                        recovery_messages.append(
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
            # OpenAI-compatible protocols require every result for one assistant tool-call
            # batch to remain contiguous. Recovery guidance belongs after the complete batch.
            run.messages.extend(recovery_messages)
            if recovery_messages:
                self.storage.save_run(run)
            if batch_succeeded:
                consecutive_rejected_batches = 0
            if publish_progress and response.content:
                await self._emit_message(run, response.content, phase="building")

    def _builder_messages(self, run: RunRecord, plan: TaskPlan) -> list[dict[str, Any]]:
        evidence = self._planning_evidence(run.messages)
        task_context = (
            f"Current request:\n{self._current_request(run)}\n\n"
            "Plan:\n"
            f"{plan.markdown}\n\nStructured contract:\n"
            f"{boundary_safe_json_dumps(plan.model_dump(mode='json'))}"
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
                f"### {name}({boundary_safe_json_dumps(arguments, sort_keys=True)})\n"
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
            {"role": "user", "content": boundary_safe_json_dumps(evidence)},
        ]
        verifier_tools = self._verifier_tools()
        for _ in range(8):
            decorated, protected_count, inserted = (
                self._messages_with_workspace_instructions(run, messages)
            )
            prepared, compacted = self.context.prepare(
                decorated,
                verifier_tools,
                protected_count=protected_count,
            )
            if compacted:
                messages = self._strip_workspace_instruction_message(
                    prepared,
                    inserted=inserted,
                )
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
                    call.name in {"list_files", "read_file", "search_text"} for call in read_calls
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
                            "content": boundary_safe_json_dumps(
                                ToolResult(
                                    tool_call_id=submit_calls[0].id,
                                    name="submit_verification",
                                    ok=False,
                                    error=f"Invalid verification report: {exc}",
                                ).model_dump(mode="json")
                            ),
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
                    result = self._redact_result(await self.tools.execute(run.id, call))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.tool_call_id,
                        "name": result.name,
                        "content": boundary_safe_json_dumps(
                            result.model_dump(mode="json")
                        ),
                    }
                )
            if submit_calls and (read_calls or len(submit_calls) > 1):
                for call in submit_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": boundary_safe_json_dumps(
                                ToolResult(
                                    tool_call_id=call.id,
                                    name=call.name,
                                    ok=False,
                                    error=(
                                        "Submit exactly one verdict in a separate turn after reads."
                                    ),
                                ).model_dump(mode="json")
                            ),
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
        async with self._lifecycle_lock(run.id):
            self._validate_transition(run, RunState.SUCCEEDED)
            if not await self._prepare_terminal_cleanup(run, RunState.SUCCEEDED):
                await self._abandon_open_output_streams(
                    run,
                    status="interrupted",
                    reason="terminal_cleanup_interrupted",
                )
                return
            previous = run.state
            if run.plan:
                for step in run.plan.steps:
                    step.status = "completed"
            turn = self._active_turn(run)
            if turn.outcome != "in_progress":
                raise RuntimeError("The successful turn was already closed")
            turn.outcome = "succeeded"
            turn.summary = self._redact(turn.summary or report.summary)[:4_000]
            turn.completed_at = utc_now()
            run.state = RunState.SUCCEEDED
            completion_payload = {
                "state": RunState.SUCCEEDED.value,
                "verification": report.model_dump(mode="json"),
                "diff": self.workspace.diff(run.id),
            }
            terminal_events, _pack = self.storage.commit_success(
                run,
                previous_state=previous,
                turn_payload={
                    "index": turn.index,
                    "outcome": "succeeded",
                    "summary": turn.summary,
                    "changed_files": turn.changed_files,
                    "approval_mode": turn.approval_mode.value,
                    "reasoning_effort": turn.reasoning_effort.value,
                    **(
                        {"final_stream_id": turn.summary_stream_id}
                        if turn.summary_stream_id
                        else {}
                    ),
                },
                completion_payload=completion_payload,
                proof_factory=lambda events: build_success_proof_pack(run, self.storage, events),
            )
            for event in terminal_events:
                await self.broker.publish(event)

    async def _complete_answer(
        self,
        run: RunRecord,
        content: str,
        *,
        final_stream_id: str | None = None,
    ) -> None:
        async with self._lifecycle_lock(run.id):
            await self._commit_terminal_turn(
                run,
                RunState.ANSWERED,
                "answered",
                content,
                completion_payload={"state": RunState.ANSWERED.value},
                final_stream_id=final_stream_id,
            )

    async def _fail(self, run: RunRecord, error: str) -> None:
        await self._abandon_open_output_streams(
            run,
            status="failed",
            reason="run_failed",
        )
        await self._abandon_active_decision(run, cause="run_failed")
        run.error = error
        self.storage.save_run(run)
        await self.broker.emit(run.id, EventType.ERROR, {"message": error})
        await self._commit_terminal_turn(
            run,
            RunState.FAILED,
            "failed",
            error,
            completion_payload={"state": RunState.FAILED.value, "error": error},
        )

    async def _interrupt_for_provider(self, run: RunRecord, error: str, category: str) -> None:
        run.error = (
            f"{error} The workspace and run history were preserved. "
            "Check the connection or model settings, then resume."
        )
        await self._transition(
            run,
            RunState.INTERRUPTED,
            interruption_reason="model_unavailable",
            interruption_error_payload={
                "message": run.error,
                "cause": "model_unavailable",
                "category": category,
                "recoverable": True,
            },
        )

    async def _request_model(
        self,
        run: RunRecord,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        request_payload = {"messages": messages, "tools": tools}
        if contains_redactable_json_secret(
            request_payload,
            api_key=self.settings.api_key,
        ) or contains_redactable_serialized_json_secret(
            request_payload,
            api_key=self.settings.api_key,
        ) or contains_compact_serialized_json_secret(
            request_payload,
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Model request contains credential-like data and was blocked before provider "
                "transmission",
                category="credential_boundary",
            )
        attempts = self.settings.model_retry_attempts
        delay = self.settings.model_retry_delay
        capability = self._reasoning_capability()
        effort = self._active_turn(run).reasoning_effort
        capability.validate(effort)
        wire_effort = (
            None
            if effort is ReasoningEffort.AUTO
            or (capability.transport == "deepseek_chat" and effort is ReasoningEffort.NONE)
            else effort.value
        )
        for attempt in range(1, attempts + 1):
            output_stream = self._new_output_stream(run, tools, attempt=attempt)
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
                if output_stream is not None:
                    stream_provider = cast(StreamingModelProvider, self.provider)
                    if effort is ReasoningEffort.AUTO:
                        response = await stream_provider.stream_complete(
                            messages,
                            tools,
                            on_delta=output_stream.on_delta,
                        )
                    else:
                        response = await stream_provider.stream_complete(
                            messages,
                            tools,
                            reasoning_effort=effort,
                            on_delta=output_stream.on_delta,
                        )
                elif effort is ReasoningEffort.AUTO:
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
                safe_response = self._canonicalize_provider_response(response)
                if output_stream is not None:
                    safe_response.output_stream_id = await output_stream.resolve(response)
                return safe_response
            except asyncio.CancelledError:
                if output_stream is not None:
                    await asyncio.shield(
                        output_stream.abort(
                            status="interrupted" if self._shutting_down else "cancelled",
                            reason=(
                                "process_shutdown" if self._shutting_down else "user_cancelled"
                            ),
                        )
                    )
                raise
            except ProviderError as exc:
                if output_stream is not None:
                    await output_stream.abort(
                        status=(
                            "retrying"
                            if exc.retryable and attempt < attempts
                            else ("interrupted" if exc.retryable else "failed")
                        ),
                        reason=exc.category,
                    )
                if not exc.retryable or attempt >= attempts:
                    raise
                retry_delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None
                    else delay
                )
                await self.broker.emit(
                    run.id,
                    EventType.MODEL_RETRY,
                    {
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                        "max_attempts": attempts,
                        "category": exc.category,
                        "delay_seconds": retry_delay,
                    },
                )
                await asyncio.sleep(retry_delay)
                delay *= 2
            except Exception as exc:
                if output_stream is not None:
                    await output_stream.abort(
                        status="failed",
                        reason="provider_contract",
                    )
                raise ProviderError(
                    f"Model provider failed unexpectedly ({type(exc).__name__})",
                    category="provider_contract",
                ) from exc
        raise AssertionError("Model retry loop ended unexpectedly")

    def _new_output_stream(
        self,
        run: RunRecord,
        tools: list[dict[str, Any]],
        *,
        attempt: int,
    ) -> _AssistantOutputStream | None:
        if not getattr(self.provider, "supports_streaming", False):
            return None
        names = {
            str(schema.get("function", {}).get("name", ""))
            for schema in tools
            if isinstance(schema.get("function"), dict)
        }
        target: _StreamTarget | None = None
        if run.state is RunState.PLANNING and "respond_to_user" in names:
            target = _StreamTarget("respond_to_user", "content")
        elif run.state is RunState.EXECUTING and "finish" in names:
            target = _StreamTarget("finish", "summary")
        if target is None:
            return None
        return _AssistantOutputStream(
            self.broker,
            run_id=run.id,
            turn_index=self._active_turn(run).index,
            phase=run.state.value,
            attempt=attempt,
            target=target,
            api_key=self.settings.api_key,
        )

    async def _complete_model(self, run: RunRecord, tools: list[dict[str, Any]]) -> ModelResponse:
        decorated, protected_count, inserted = self._messages_with_workspace_instructions(
            run,
            run.messages,
        )
        prepared, compacted = self.context.prepare(
            decorated,
            tools,
            protected_count=protected_count,
        )
        if compacted:
            run.messages = self._strip_workspace_instruction_message(
                prepared,
                inserted=inserted,
            )
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

    def _canonicalize_provider_response(self, response: ModelResponse) -> ModelResponse:
        """Make provider-controlled output safe before any semantic use or persistence."""

        if (
            not isinstance(response.content, str)
            or not isinstance(response.preserve_empty_content, bool)
            or (
                response.reasoning_content is not None
                and not isinstance(response.reasoning_content, str)
            )
            or (response.finish_reason is not None and not isinstance(response.finish_reason, str))
            or response.output_stream_id is not None
        ):
            raise ProviderError(
                "Model provider returned an invalid response object",
                category="provider_contract",
            )
        if response.reasoning_content is not None and contains_redactable_secret(
            response.reasoning_content,
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Provider-private replay state could not be stored safely",
                category="provider_contract",
            )
        if response.finish_reason is not None and contains_redactable_secret(
            response.finish_reason,
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Provider response metadata contained credential-like data",
                category="provider_contract",
            )

        safe_calls: list[ToolCall] = []
        for call in response.tool_calls:
            if not isinstance(call, ToolCall):
                raise ProviderError(
                    "Model provider returned an invalid tool call",
                    category="provider_contract",
                )
            safe_calls.append(self._canonicalize_provider_tool_call(call))
        safe_response = ModelResponse(
            content=self._redact(response.content),
            tool_calls=safe_calls,
            finish_reason=response.finish_reason,
            reasoning_content=response.reasoning_content,
            preserve_empty_content=response.preserve_empty_content,
        )
        if contains_redactable_serialized_json_secret(
            safe_response.as_assistant_message(),
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Provider response could not be serialized without credential-like data",
                category="provider_contract",
            )
        return safe_response

    def _canonicalize_provider_tool_call(self, call: ToolCall) -> ToolCall:
        if contains_redactable_secret(
            call.id, api_key=self.settings.api_key
        ) or contains_redactable_secret(call.name, api_key=self.settings.api_key):
            raise ProviderError(
                "Provider tool call contained credential-like data and was rejected before "
                "storage or execution",
                category="provider_contract",
            )
        if call.name in {"respond_to_user", "finish"}:
            try:
                arguments = cast(
                    dict[str, Any],
                    redact_json_value(call.arguments, api_key=self.settings.api_key),
                )
            except ValueError as exc:
                raise ProviderError(
                    "Provider tool call could not be redacted safely",
                    category="provider_contract",
                ) from exc
        else:
            if contains_redactable_json_secret(
                call.arguments,
                api_key=self.settings.api_key,
            ):
                raise ProviderError(
                    "Provider tool call contained credential-like data and was rejected before "
                    "storage or execution",
                    category="provider_contract",
                )
            arguments = call.model_copy(deep=True).arguments
        safe_call = ToolCall(id=call.id, name=call.name, arguments=arguments)
        if contains_redactable_serialized_json_secret(
            safe_call.model_dump(mode="json"),
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Provider tool call could not be serialized without credential-like data",
                category="provider_contract",
            )
        return safe_call

    def _public_tool_call_payload(self, call: ToolCall) -> dict[str, Any]:
        return self._canonicalize_provider_tool_call(call).model_dump(mode="json")

    def _require_safe_action_approval(self, approval: ApprovalRequest) -> None:
        if contains_redactable_json_secret(
            approval.model_dump(mode="json"),
            api_key=self.settings.api_key,
        ) or contains_redactable_serialized_json_secret(
            approval.model_dump(mode="json"),
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Persisted action approval contains credential-like data and cannot be resumed",
                category="provider_contract",
            )
        self._canonicalize_provider_tool_call(approval.tool_call)

    def _reject_credential_input(self, value: Any, *, label: str, action: str) -> None:
        if contains_redactable_json_secret(
            value, api_key=self.settings.api_key
        ) or contains_redactable_serialized_json_secret(
            value, api_key=self.settings.api_key
        ):
            raise ValueError(
                f"{label} contains credential-like data; remove it before {action}"
            )

    def _require_safe_persisted_context(
        self,
        run: RunRecord,
        *,
        instruction_snapshot: WorkspaceInstructionSnapshot | None = None,
    ) -> None:
        if self._persisted_context_is_unsafe(
            run,
            instruction_snapshot=instruction_snapshot,
        ):
            recovery = (
                "Restore the previous credential, or stop this interrupted turn before "
                "starting a new task."
                if run.state is RunState.INTERRUPTED
                else "Restore the previous credential or start a new task."
            )
            raise InvalidRunAction(
                "This task's stored context conflicts with the current provider credential and "
                f"cannot be sent to the model. {recovery}"
            )

    def _persisted_context_is_unsafe(
        self,
        run: RunRecord,
        *,
        instruction_snapshot: WorkspaceInstructionSnapshot | None = None,
    ) -> bool:
        active_decision = self.storage.get_active_decision(run.id)
        serialized = {
            "run": run.model_dump(mode="json"),
            "workspace_instruction_snapshot": (
                instruction_snapshot.model_dump(mode="json")
                if instruction_snapshot is not None
                else None
            ),
            "events": [
                event.model_dump(mode="json")
                for event in self.storage.get_events(run.id)
            ],
            "active_decision": (
                active_decision.model_dump(mode="json")
                if active_decision is not None
                else None
            ),
        }
        if contains_redactable_json_secret(
            serialized,
            api_key=self.settings.api_key,
        ) or contains_redactable_serialized_json_secret(
            serialized,
            api_key=self.settings.api_key,
        ) or contains_compact_serialized_json_secret(
            serialized,
            api_key=self.settings.api_key,
        ):
            return True
        if instruction_snapshot is None:
            return False
        for surface in self._persisted_model_request_surfaces(
            run,
            instruction_snapshot,
        ):
            if contains_redactable_json_secret(
                surface,
                api_key=self.settings.api_key,
            ) or contains_redactable_serialized_json_secret(
                surface,
                api_key=self.settings.api_key,
            ) or contains_compact_serialized_json_secret(
                surface,
                api_key=self.settings.api_key,
            ):
                return True
        return False

    async def _await_clarification(
        self,
        run: RunRecord,
        *,
        existing: DecisionRequest | None = None,
    ) -> tuple[str, list[ClarificationAnswer]]:
        assert run.clarification is not None
        future: asyncio.Future[list[ClarificationAnswer]] = (
            asyncio.get_running_loop().create_future()
        )
        request_id = existing.request_id if existing else uuid4().hex
        control = self._control(run.id)
        control.clarification_future = future
        control.decision_request_id = request_id
        control.decision_kind = DecisionKind.CLARIFICATION
        requested_payload = run.clarification.model_dump(mode="json")
        if existing is None:
            previous = run.state
            self._validate_transition(run, RunState.AWAITING_CLARIFICATION)
            run.state = RunState.AWAITING_CLARIFICATION
            _receipt, events = self.storage.open_decision(
                run,
                previous_state=previous,
                request_id=request_id,
                kind=DecisionKind.CLARIFICATION,
                turn_index=self._active_turn(run).index,
                subject=requested_payload,
                requested_event_type=EventType.CLARIFICATION_REQUESTED,
                requested_payload=requested_payload,
            )
            await self._publish_persisted(events)
        elif existing.status is DecisionStatus.PENDING:
            previous = run.state
            self._validate_transition(run, RunState.AWAITING_CLARIFICATION)
            run.state = RunState.AWAITING_CLARIFICATION
            _receipt, events = self.storage.reopen_decision(
                run,
                request_id,
                previous_state=previous,
                requested_event_type=EventType.CLARIFICATION_REQUESTED,
                requested_payload=requested_payload,
            )
            await self._publish_persisted(events)
        self._signal_decision(self.storage.get_decision(run.id, request_id))
        return request_id, await future

    async def _apply_clarification_decision(
        self,
        run: RunRecord,
        request_id: str,
        answers: list[ClarificationAnswer],
        *,
        tool_call_id: str | None = None,
    ) -> None:
        previous = run.state
        self._append_clarification_answers(run, answers, tool_call_id)
        run.clarification = None
        self._validate_transition(run, RunState.PLANNING)
        run.state = RunState.PLANNING
        _receipt, events = self.storage.consume_decision(
            run,
            request_id,
            DecisionKind.CLARIFICATION,
            previous_state=previous,
            resolved_event_type=EventType.CLARIFICATION_ANSWERED,
            resolved_payload={"answers": [answer.model_dump(mode="json") for answer in answers]},
        )
        await self._publish_persisted(events)

    async def _await_plan_decision(
        self,
        run: RunRecord,
        *,
        existing: DecisionRequest | None = None,
    ) -> tuple[str, PlanDecision]:
        future: asyncio.Future[PlanDecision] = asyncio.get_running_loop().create_future()
        assert run.plan is not None
        request_id = existing.request_id if existing else uuid4().hex
        control = self._control(run.id)
        control.plan_future = future
        control.decision_request_id = request_id
        control.decision_kind = DecisionKind.PLAN
        requested_payload = run.plan.model_dump(mode="json")
        if existing is None:
            previous = run.state
            self._validate_transition(run, RunState.AWAITING_PLAN_APPROVAL)
            run.state = RunState.AWAITING_PLAN_APPROVAL
            _receipt, events = self.storage.open_decision(
                run,
                previous_state=previous,
                request_id=request_id,
                kind=DecisionKind.PLAN,
                turn_index=self._active_turn(run).index,
                subject=requested_payload,
                requested_event_type=EventType.PLAN_UPDATED,
                requested_payload=requested_payload,
            )
            await self._publish_persisted(events)
        elif existing.status is DecisionStatus.PENDING:
            previous = run.state
            self._validate_transition(run, RunState.AWAITING_PLAN_APPROVAL)
            run.state = RunState.AWAITING_PLAN_APPROVAL
            _receipt, events = self.storage.reopen_decision(
                run,
                request_id,
                previous_state=previous,
                requested_event_type=EventType.PLAN_UPDATED,
                requested_payload=requested_payload,
            )
            await self._publish_persisted(events)
        self._signal_decision(self.storage.get_decision(run.id, request_id))
        return request_id, await future

    async def _apply_plan_decision(
        self,
        run: RunRecord,
        request_id: str,
        decision: PlanDecision,
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        previous = run.state
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
            assert run.plan is not None
            run.plan_approved = True
            run.messages = self._builder_messages(run, run.plan)
            self._validate_transition(run, RunState.EXECUTING)
            run.state = RunState.EXECUTING
            _receipt, events = self.storage.consume_decision(
                run,
                request_id,
                DecisionKind.PLAN,
                previous_state=previous,
                resolved_event_type=EventType.PLAN_RESOLVED,
                resolved_payload=decision.model_dump(mode="json"),
            )
            await self._publish_persisted(events)
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
        self._validate_transition(run, RunState.PLANNING)
        run.state = RunState.PLANNING
        _receipt, events = self.storage.consume_decision(
            run,
            request_id,
            DecisionKind.PLAN,
            previous_state=previous,
            resolved_event_type=EventType.PLAN_RESOLVED,
            resolved_payload=decision.model_dump(mode="json"),
        )
        await self._publish_persisted(events)
        return False

    async def _await_action_approval(
        self, run: RunRecord, call: ToolCall, permission: PermissionResolution
    ) -> tuple[bool, str, ToolResult | None]:
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
        self._require_safe_action_approval(approval)
        run.pending_approval = approval
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        control = self._control(run.id)
        control.approval_future = future
        control.decision_request_id = approval.id
        control.decision_kind = DecisionKind.ACTION
        previous = run.state
        self._validate_transition(run, RunState.AWAITING_ACTION_APPROVAL)
        run.state = RunState.AWAITING_ACTION_APPROVAL
        _receipt, events = self.storage.open_decision(
            run,
            previous_state=previous,
            request_id=approval.id,
            kind=DecisionKind.ACTION,
            turn_index=self._active_turn(run).index,
            subject=approval.model_dump(mode="json"),
            requested_event_type=EventType.APPROVAL_REQUESTED,
            requested_payload={
                **approval.model_dump(mode="json"),
                "mode": permission.mode.value,
                "policy_decision": permission.policy_decision.value,
            },
        )
        await self._publish_persisted(events)
        self._signal_decision(self.storage.get_decision(run.id, approval.id))
        approved = await future
        persisted_result = await self._consume_action_decision(run, approval, permission, approved)
        return approved, approval.id, persisted_result

    async def _consume_action_decision(
        self,
        run: RunRecord,
        approval: ApprovalRequest,
        permission: PermissionResolution,
        approved: bool,
    ) -> ToolResult | None:
        self._require_safe_action_approval(approval)
        previous = run.state
        run.pending_approval = None
        self._validate_transition(run, RunState.EXECUTING)
        run.state = RunState.EXECUTING
        completed_result: ToolResult | None = None
        if not approved:
            completed_result = ToolResult(
                tool_call_id=approval.tool_call.id,
                name=approval.tool_call.name,
                ok=False,
                error="User rejected this action.",
                metadata={
                    "permission": permission.as_metadata(
                        outcome="user_rejected", sandbox_bypass=False
                    )
                },
            )
        elif permission.decision is PermissionDecision.DENY:
            completed_result = ToolResult(
                tool_call_id=approval.tool_call.id,
                name=approval.tool_call.name,
                ok=False,
                error=(
                    "The accepted action was not resumed because the current invariant "
                    f"permission policy denies it: {permission.reason}"
                ),
                metadata={
                    "permission": permission.as_metadata(
                        outcome="denied_after_resume", sandbox_bypass=False
                    )
                },
            )
        if completed_result is not None:
            completed_result = self._redact_result(completed_result)
            self._append_tool_result(run, completed_result)
        _receipt, events = self.storage.consume_decision(
            run,
            approval.id,
            DecisionKind.ACTION,
            previous_state=previous,
            resolved_event_type=EventType.APPROVAL_RESOLVED,
            resolved_payload={
                "approval_id": approval.id,
                "approved": approved,
                "outcome": "approved" if approved else "rejected",
                "mode": permission.mode.value,
                "sandbox_bypass": (permission.sandbox_bypass_on_allow if approved else False),
            },
            action_call_payload=(
                self._public_tool_call_payload(approval.tool_call)
                if approved and permission.decision is not PermissionDecision.DENY
                else None
            ),
            completed_tool_payload=(
                {
                    "call": self._public_tool_call_payload(approval.tool_call),
                    "result": completed_result.model_dump(mode="json"),
                    "approval_request_id": approval.id,
                }
                if completed_result is not None
                else None
            ),
        )
        await self._publish_persisted(events)
        return completed_result

    async def _resume_action_decision(self, run: RunRecord, existing: DecisionRequest) -> None:
        approval = run.pending_approval
        if approval is None or existing.request_id != approval.id:
            raise RuntimeError("Persisted action decision does not match its approval")
        self._require_safe_action_approval(approval)
        self._require_decision_subject(existing, approval.model_dump(mode="json"))
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        control = self._control(run.id)
        control.approval_future = future
        control.decision_request_id = approval.id
        control.decision_kind = DecisionKind.ACTION
        requested_payload = {
            **approval.model_dump(mode="json"),
            "mode": approval.approval_mode.value,
            "policy_decision": approval.policy_decision,
        }
        if existing.status is DecisionStatus.PENDING:
            previous = run.state
            self._validate_transition(run, RunState.AWAITING_ACTION_APPROVAL)
            run.state = RunState.AWAITING_ACTION_APPROVAL
            _receipt, events = self.storage.reopen_decision(
                run,
                approval.id,
                previous_state=previous,
                requested_event_type=EventType.APPROVAL_REQUESTED,
                requested_payload=requested_payload,
            )
            await self._publish_persisted(events)
        self._signal_decision(self.storage.get_decision(run.id, approval.id))
        approved = await future
        permission = self.tools.resolve_permission(
            approval.tool_call, run.plan, approval.approval_mode
        )
        persisted_result = await self._consume_action_decision(run, approval, permission, approved)
        if persisted_result is None:
            sandbox_bypass = approval.sandbox_bypass_on_approve
            result = await self.tools.execute(
                run.id,
                approval.tool_call,
                sandbox_bypass=sandbox_bypass,
            )
            result.metadata["permission"] = permission.as_metadata(
                outcome="user_approved_after_resume",
                sandbox_bypass=sandbox_bypass,
            )
            result = self._redact_result(result)
            self._append_tool_result(run, result)
        else:
            result = persisted_result
        await self._update_checks_and_diff(run, approval.tool_call, result)
        if persisted_result is None:
            await self._emit_tool_result(
                run,
                approval.tool_call,
                result,
                approval_request_id=approval.id,
            )
        self.storage.save_run(run)

    async def _abandon_pending_approval(self, run: RunRecord, *, cause: str) -> None:
        approval = run.pending_approval
        if approval is None:
            return
        try:
            self._require_safe_action_approval(approval)
        except ProviderError:
            run.pending_approval = None
            run.messages = []
            self.storage.save_run(run)
            await self.broker.emit(
                run.id,
                EventType.DECISION_ABANDONED,
                {"kind": DecisionKind.ACTION.value, "cause": cause},
            )
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

    async def _abandon_active_decision(self, run: RunRecord, *, cause: str) -> None:
        receipt = self.storage.get_active_decision(run.id)
        if receipt is None:
            await self._abandon_pending_approval(run, cause=cause)
            if run.clarification is not None:
                run.clarification = None
                self.storage.save_run(run)
            return
        unsafe_action = False
        if receipt.kind is DecisionKind.ACTION and run.pending_approval is not None:
            approval = run.pending_approval
            try:
                self._require_safe_action_approval(approval)
            except ProviderError:
                unsafe_action = True
                run.messages = []
                event_type = EventType.DECISION_ABANDONED
                event_payload = {"cause": cause, "unsafe_subject_discarded": True}
            else:
                event_type = EventType.APPROVAL_RESOLVED
                event_payload = {
                    "approval_id": approval.id,
                    "approved": False,
                    "outcome": "abandoned",
                    "cause": cause,
                    "mode": approval.approval_mode.value,
                    "sandbox_bypass": False,
                }
            run.pending_approval = None
        else:
            if receipt.kind is DecisionKind.CLARIFICATION:
                run.clarification = None
            event_type = EventType.DECISION_ABANDONED
            event_payload = {"cause": cause}
        try:
            _receipt, event = self.storage.abandon_decision(
                run,
                receipt.request_id,
                event_type=event_type,
                event_payload=event_payload,
                include_request_id=not unsafe_action,
            )
        except DecisionConflictError:
            return
        await self._publish_persisted([event])

    async def _transition(
        self,
        run: RunRecord,
        new_state: RunState,
        *,
        interruption_reason: str | None = None,
        interruption_error_payload: dict[str, Any] | None = None,
    ) -> bool:
        same_state = run.state is new_state
        self._validate_transition(run, new_state)
        if new_state is RunState.INTERRUPTED:
            if same_state:
                await self._abandon_open_output_streams(
                    run,
                    status="interrupted",
                    reason=interruption_reason or "run_interrupted",
                )
                return True
            previous = run.state
            run.interrupted_from = previous
            run.state = RunState.INTERRUPTED
            if run.turns:
                run.turns[-1].summary_stream_id = None
            cause = interruption_reason or "run_interrupted"
            events = self.storage.commit_interruption(
                run,
                previous_state=previous,
                stream_status="interrupted",
                stream_reason=cause,
                state_payload={
                    "state": RunState.INTERRUPTED.value,
                    "previous": previous.value,
                    "cause": cause,
                },
                error_payload=interruption_error_payload,
            )
            await self._publish_persisted(events)
            return True
        if new_state.terminal and not await self._prepare_terminal_cleanup(run, new_state):
            return False
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

    async def _prepare_terminal_cleanup(self, run: RunRecord, intended_state: RunState) -> bool:
        # Persist and physically checkpoint the scrub while the row is still recoverable. A
        # crash or busy WAL can then leave a nonterminal/interrupted row, never a terminal row
        # that still contains provider-private replay state.
        scrubbed = self._scrub_provider_reasoning(run)
        run.provider_reasoning_cleanup_pending = run.provider_reasoning_cleanup_pending or scrubbed
        self.storage.save_run(run)
        if not run.provider_reasoning_cleanup_pending:
            return True
        try:
            self.storage.secure_checkpoint()
        except SecureCheckpointError:
            await self._interrupt_for_reasoning_cleanup(run, intended_state)
            return False
        run.provider_reasoning_cleanup_pending = False
        self.storage.save_run(run)
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
        if run.turns:
            run.turns[-1].summary_stream_id = None
        events = self.storage.commit_interruption(
            run,
            previous_state=previous,
            stream_status="interrupted",
            stream_reason="provider_reasoning_cleanup_pending",
            error_payload={
                "message": run.error,
                "cause": "provider_reasoning_cleanup_pending",
                "category": "storage_cleanup",
                "recoverable": True,
                "intended_state": intended_state.value,
            },
            state_payload={
                "state": RunState.INTERRUPTED.value,
                "previous": previous.value,
                "cause": "provider_reasoning_cleanup_pending",
            },
        )
        await self._publish_persisted(events)

    def _append_clarification_answers(
        self,
        run: RunRecord,
        answers: list[ClarificationAnswer],
        tool_call_id: str | None = None,
    ) -> None:
        content = boundary_safe_json_dumps(
            {"answers": [answer.model_dump(mode="json") for answer in answers]}
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

    def _append_tool_result(self, run: RunRecord, result: ToolResult) -> None:
        model_result = self._redact_result(result.model_copy(deep=True))
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
                "content": boundary_safe_json_dumps(
                    model_result.model_dump(mode="json")
                ),
            }
        )

    def _assistant_message_for_storage(self, response: ModelResponse) -> dict[str, Any]:
        message = response.as_assistant_message()
        private_reasoning = message.pop("reasoning_content", None)
        if response.tool_calls:
            wire_calls = message.get("tool_calls")
            if not isinstance(wire_calls, list) or len(wire_calls) != len(response.tool_calls):
                raise ProviderError(
                    "Provider tool calls could not be stored safely",
                    category="provider_contract",
                )
            for wire_call, source_call in zip(wire_calls, response.tool_calls, strict=True):
                if not isinstance(wire_call, dict):
                    raise ProviderError(
                        "Provider tool calls could not be stored safely",
                        category="provider_contract",
                    )
                function = wire_call.get("function")
                if not isinstance(function, dict):
                    raise ProviderError(
                        "Provider tool calls could not be stored safely",
                        category="provider_contract",
                    )
                try:
                    safe_arguments = redact_json_value(
                        source_call.arguments,
                        api_key=self.settings.api_key,
                    )
                except ValueError as exc:
                    raise ProviderError(
                        "Provider tool calls could not be stored safely",
                        category="provider_contract",
                    ) from exc
                function["arguments"] = boundary_safe_json_dumps(safe_arguments)
        try:
            stored = cast(
                dict[str, Any],
                redact_json_value(message, api_key=self.settings.api_key),
            )
        except ValueError as exc:
            raise ProviderError(
                "Provider response could not be stored safely",
                category="provider_contract",
            ) from exc
        if private_reasoning is not None:
            if contains_redactable_secret(
                private_reasoning,
                api_key=self.settings.api_key,
            ):
                raise ProviderError(
                    "Provider-private replay state could not be stored safely",
                    category="provider_contract",
                )
            stored["reasoning_content"] = private_reasoning
        if contains_redactable_serialized_json_secret(
            stored,
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Provider response could not be stored without credential-like data",
                category="provider_contract",
            )
        return stored

    def _event_for_model(self, event: RunEvent) -> dict[str, Any]:
        payload = cast(
            dict[str, Any],
            self.storage.redact_public_value(event.model_dump(mode="json")),
        )
        result = payload.get("payload", {}).get("result")
        if isinstance(result, dict):
            for field in ("output", "error"):
                value = result.get(field)
                if isinstance(value, str):
                    result[field] = _limit_for_model(value, self.settings.model_output_limit)
        return payload

    def _append_tool_error(self, run: RunRecord, call: ToolCall, error: str) -> None:
        self._append_tool_result(
            run,
            ToolResult(tool_call_id=call.id, name=call.name, ok=False, error=error),
        )

    async def _reject_builder_batch(
        self,
        run: RunRecord,
        calls: list[ToolCall],
        *,
        error: str,
        correction: str,
    ) -> None:
        results: list[tuple[ToolCall, ToolResult]] = []
        for call in calls:
            result = self._redact_result(
                ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    ok=False,
                    error=error,
                    metadata={
                        "outcome": "protocol_rejected",
                        "execution": "not_started",
                    },
                )
            )
            self._append_tool_result(run, result)
            results.append((call, result))
        run.messages.append({"role": "user", "content": correction})
        self.storage.save_run(run)
        for call, result in results:
            await self.broker.emit(
                run.id,
                EventType.TOOL_REQUESTED,
                self._public_tool_call_payload(call),
            )
            await self._emit_tool_result(run, call, result)

    async def _reject_invalid_planning_call(
        self,
        run: RunRecord,
        call: ToolCall,
        *,
        label: str,
        error: ValidationError,
    ) -> None:
        details = boundary_safe_json_dumps(
            error.errors(include_url=False, include_input=False)
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
        self,
        run: RunRecord,
        call: ToolCall,
        result: ToolResult,
        *,
        approval_request_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "call": self._public_tool_call_payload(call),
            "result": self._redact_result(result.model_copy(deep=True)).model_dump(mode="json"),
        }
        if approval_request_id is not None:
            payload["approval_request_id"] = approval_request_id
        await self.broker.emit(
            run.id,
            EventType.TOOL_COMPLETED,
            payload,
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
        await self.broker.emit(run.id, EventType.PLAN_UPDATED, run.plan.model_dump(mode="json"))

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
        if contains_redactable_secret(
            result.tool_call_id, api_key=self.settings.api_key
        ) or contains_redactable_secret(result.name, api_key=self.settings.api_key):
            raise ProviderError(
                "Tool result identity contained credential-like data",
                category="tool_contract",
            )
        result.output = self._redact(result.output)
        if result.error:
            result.error = self._redact(result.error)
        try:
            result.metadata = cast(
                dict[str, Any],
                redact_json_value(result.metadata, api_key=self.settings.api_key),
            )
        except ValueError as exc:
            raise ProviderError(
                "Tool result metadata could not be redacted safely",
                category="tool_contract",
            ) from exc
        if contains_redactable_serialized_json_secret(
            result.model_dump(mode="json"),
            api_key=self.settings.api_key,
        ):
            raise ProviderError(
                "Tool result could not be serialized without credential-like data",
                category="tool_contract",
            )
        return result

    def _redact(self, text: str) -> str:
        return redact_text(text, api_key=self.settings.api_key)

    def _control(self, run_id: str) -> _Control:
        try:
            return self._controls[run_id]
        except KeyError as exc:
            raise InvalidRunAction(f"Run is not active: {run_id}") from exc

    @staticmethod
    def _validate_transition(run: RunRecord, new_state: RunState) -> None:
        if new_state is not run.state and new_state not in _ALLOWED_TRANSITIONS[run.state]:
            raise RuntimeError(f"Invalid run transition: {run.state.value} -> {new_state.value}")

    async def _publish_persisted(self, events: list[RunEvent]) -> None:
        for event in events:
            await self.broker.publish(event)

    @staticmethod
    def _workspace_instruction_events(
        snapshot: WorkspaceInstructionSnapshot,
        *,
        turn_index: int,
    ) -> list[tuple[EventType, dict[str, Any]]]:
        if not snapshot.sources:
            return []
        payload = snapshot.manifest().model_dump(mode="json")
        payload.update(
            {
                "turn_index": turn_index,
                "status": "loaded",
                "authority": "guidance",
                "content_private": True,
            }
        )
        return [(EventType.WORKSPACE_INSTRUCTIONS_RESOLVED, payload)]

    def _workspace_instruction_snapshot(
        self,
        run: RunRecord,
    ) -> WorkspaceInstructionSnapshot:
        turn_index = self._active_turn(run).index
        try:
            snapshot = self.storage.get_workspace_instruction_snapshot(run.id, turn_index)
        except KeyError as exc:
            raise InvalidRunAction(
                "This legacy turn has no immutable workspace-instruction snapshot and "
                "cannot be resumed safely. Stop this interrupted turn before following up, "
                "or start a new task instead."
            ) from exc
        self.workspace_instructions.validate_for_model(snapshot)
        return snapshot

    def _stored_instruction_snapshot_for_recovery(
        self,
        run: RunRecord,
    ) -> WorkspaceInstructionSnapshot | None:
        if not run.turns:
            return None
        turn_index = max(turn.index for turn in run.turns)
        return self.storage.try_get_workspace_instruction_snapshot(run.id, turn_index)

    @staticmethod
    def _messages_with_instruction_snapshot(
        messages: list[dict[str, Any]],
        snapshot: WorkspaceInstructionSnapshot,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        context = render_workspace_instruction_context(snapshot)
        if context is None:
            return messages.copy(), 2, False
        if not messages or messages[0].get("role") != "system":
            raise RuntimeError(
                "Workspace instructions require a leading system message"
            )
        return (
            [
                messages[0],
                {"role": "user", "content": context},
                *messages[1:],
            ],
            3,
            True,
        )

    def _messages_with_workspace_instructions(
        self,
        run: RunRecord,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, bool]:
        return self._messages_with_instruction_snapshot(
            messages,
            self._workspace_instruction_snapshot(run),
        )

    def _planning_tools(self) -> list[dict[str, Any]]:
        exploration_tools = [
            schema
            for schema in self.tools.schemas
            if schema["function"]["name"] in {"list_files", "read_file", "search_text"}
        ]
        return [
            *exploration_tools,
            _model_tool(
                "respond_to_user",
                "Give a natural answer without claiming execution or verification.",
                DirectResponse.model_json_schema(),
            ),
            _model_tool(
                "ask_questions",
                "Ask material clarification questions.",
                _questions_schema(),
            ),
            _model_tool(
                "submit_plan",
                "Submit the implementation plan.",
                TaskPlan.model_json_schema(),
            ),
        ]

    def _builder_tools(self) -> list[dict[str, Any]]:
        return [
            *self.tools.schemas,
            _model_tool(
                "update_plan",
                "Update the status of one or more approved plan steps.",
                PlanUpdateRequest.model_json_schema(),
            ),
        ]

    def _verifier_tools(self) -> list[dict[str, Any]]:
        read_tools = [
            schema
            for schema in self.tools.schemas
            if schema["function"]["name"]
            in {"list_files", "read_file", "search_text"}
        ]
        return [
            *read_tools,
            _model_tool(
                "submit_verification",
                "Submit the independent verification verdict.",
                VerificationReport.model_json_schema(),
            ),
        ]

    def _persisted_model_request_surfaces(
        self,
        run: RunRecord,
        snapshot: WorkspaceInstructionSnapshot,
    ) -> list[dict[str, Any]]:
        message_variants: list[list[dict[str, Any]]] = []
        if run.messages and run.messages[0].get("role") == "system":
            message_variants.append(run.messages)
        planning_messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._conversation_context(run, self._current_request(run)),
            },
        ]
        if planning_messages not in message_variants:
            message_variants.append(planning_messages)
        if run.plan is not None:
            builder_messages = self._builder_messages(run, run.plan)
            if builder_messages not in message_variants:
                message_variants.append(builder_messages)

        tool_variants = [
            self._planning_tools(),
            self._builder_tools(),
            self._verifier_tools(),
        ]
        builder_finish_tools = [
            schema
            for schema in tool_variants[1]
            if schema["function"]["name"] == "finish"
        ]
        if builder_finish_tools:
            tool_variants.append(builder_finish_tools)

        surfaces: list[dict[str, Any]] = []
        for messages in message_variants:
            decorated, _protected_count, _inserted = (
                self._messages_with_instruction_snapshot(messages, snapshot)
            )
            surfaces.extend(
                {"messages": decorated, "tools": tools}
                for tools in tool_variants
            )
        return surfaces

    def _validate_initial_model_context(
        self,
        request: str,
        snapshot: WorkspaceInstructionSnapshot,
    ) -> None:
        if not snapshot.sources:
            return
        guidance = render_workspace_instruction_context(snapshot)
        assert guidance is not None
        self.context.prepare(
            [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": guidance},
                {"role": "user", "content": f"Current request:\n{request}"},
            ],
            self._planning_tools(),
            protected_count=3,
        )

    @staticmethod
    def _strip_workspace_instruction_message(
        messages: list[dict[str, Any]],
        *,
        inserted: bool,
    ) -> list[dict[str, Any]]:
        if not inserted:
            return messages.copy()
        if len(messages) < 2:
            raise RuntimeError("Compacted workspace instruction context is incomplete")
        return [messages[0], *messages[2:]]

    def _decision_or_reject(self, run_id: str, request_id: str) -> DecisionRequest:
        try:
            return self.storage.get_decision(run_id, request_id)
        except KeyError as exc:
            raise InvalidRunAction("Decision request is unknown or expired") from exc

    def _active_decision_id(self, run_id: str, kind: DecisionKind) -> str:
        receipt = self.storage.get_active_decision(run_id)
        if receipt is None or receipt.kind is not kind:
            label = "clarification" if kind is DecisionKind.CLARIFICATION else "plan approval"
            raise InvalidRunAction(f"The run is not waiting for {label}")
        return receipt.request_id

    def _accept_decision_or_reject(
        self,
        run_id: str,
        request_id: str,
        kind: DecisionKind,
        payload: dict[str, Any],
    ) -> DecisionRequest:
        try:
            return self.storage.accept_decision(run_id, request_id, kind, payload)
        except DecisionConflictError as exc:
            raise InvalidRunAction(str(exc)) from exc

    @staticmethod
    def _require_decision_subject(receipt: DecisionRequest, subject: dict[str, Any]) -> None:
        if receipt.subject_sha256 != decision_payload_sha256(subject):
            raise InvalidRunAction(
                "Decision request no longer matches the clarification, plan, or action shown"
            )

    def _signal_decision(self, receipt: DecisionRequest) -> None:
        if receipt.status is not DecisionStatus.ACCEPTED or receipt.payload is None:
            return
        control = self._controls.get(receipt.run_id)
        if (
            control is None
            or control.decision_request_id != receipt.request_id
            or control.decision_kind is not receipt.kind
        ):
            return
        if receipt.kind is DecisionKind.CLARIFICATION:
            clarification_future = control.clarification_future
            raw_answers = receipt.payload.get("answers")
            if (
                clarification_future is not None
                and not clarification_future.done()
                and isinstance(raw_answers, list)
            ):
                clarification_future.set_result(
                    [ClarificationAnswer.model_validate(answer) for answer in raw_answers]
                )
        elif receipt.kind is DecisionKind.PLAN:
            plan_future = control.plan_future
            if plan_future is not None and not plan_future.done():
                plan_future.set_result(PlanDecision.model_validate(receipt.payload))
        else:
            approval_future = control.approval_future
            approved = receipt.payload.get("approved")
            if (
                approval_future is not None
                and not approval_future.done()
                and isinstance(approved, bool)
            ):
                approval_future.set_result(approved)

    def _mark_uncertain_started_approvals(self, run_id: str) -> int:
        events = self.storage.get_events(run_id)
        uncertain_request_ids: set[str] = set()
        for started in events:
            if started.type is not EventType.TOOL_STARTED or not started.payload.get(
                "approval_request_id"
            ):
                continue
            request_id = str(started.payload["approval_request_id"])
            call_id = str(started.payload.get("id", ""))
            completed = any(
                event.type is EventType.TOOL_COMPLETED
                and event.seq > started.seq
                and (
                    event.payload.get("approval_request_id") == request_id
                    or (
                        event.payload.get("approval_request_id") is None
                        and isinstance(event.payload.get("call"), dict)
                        and str(event.payload["call"].get("id", "")) == call_id
                    )
                )
                for event in events
            )
            if not completed:
                uncertain_request_ids.add(request_id)
        for request_id in uncertain_request_ids:
            self.storage.mark_action_uncertain(run_id, request_id)
        return len(uncertain_request_ids)

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

    def _previous_turns_context(self, run: RunRecord) -> str:
        own_entries: list[tuple[str, ConversationTurn]] = [
            ("this run", turn) for turn in run.turns[:-1] if turn.outcome != "in_progress"
        ]
        ancestors: list[RunRecord] = []
        parent_id = self.storage.get_parent_run_id(run.id)
        for _ in range(3):
            if parent_id is None:
                break
            parent = self.storage.get_run(parent_id)
            ancestors.append(parent)
            parent_id = self.storage.get_parent_run_id(parent.id)

        entries: list[tuple[str, ConversationTurn]] = []
        for ancestor in reversed(ancestors):
            entries.extend(
                (f"rolled-back predecessor {ancestor.id[:8]}", turn)
                for turn in ancestor.turns
                if turn.outcome != "in_progress"
            )
        entries.extend(own_entries)
        if not entries:
            return ""

        lines: list[str] = []
        if ancestors:
            lines.extend(
                [
                    "Earlier task history (including rolled-back predecessor runs):",
                    "The current workspace is authoritative. Predecessor summaries preserve "
                    "intent and historical evidence only; do not assume their file changes "
                    "still exist.",
                ]
            )
        else:
            lines.append("Earlier turns in this same task:")
        for source, turn in entries[-6:]:
            source_suffix = "" if source == "this run" else f" [{source}]"
            lines.append(
                f"- Turn {turn.index}{source_suffix}: {turn.request}\n"
                f"  Outcome: {turn.outcome}. Summary: {turn.summary or '(no summary)'}"
            )
        return "\n".join(lines)

    def _conversation_context(self, run: RunRecord, request: str) -> str:
        previous = self._previous_turns_context(run)
        current = f"Current request:\n{request}"
        return f"{previous}\n\n{current}" if previous else current

    def _set_turn_summary(
        self,
        run: RunRecord,
        summary: str,
        *,
        stream_id: str | None = None,
    ) -> None:
        if summary:
            turn = self._active_turn(run)
            turn.summary = self._redact(summary)[:4_000]
            turn.summary_stream_id = stream_id
            self.storage.save_run(run)

    async def _emit_output_abort(
        self,
        run: RunRecord,
        stream_id: str,
        *,
        status: str,
        reason: str,
    ) -> None:
        await self.broker.abort_open_assistant_outputs(
            run.id,
            status=status,
            reason=reason,
            stream_id=stream_id,
        )

    async def _abandon_open_output_streams(
        self,
        run: RunRecord,
        *,
        status: str,
        reason: str,
    ) -> None:
        turn = self._active_turn(run)
        await self.broker.abort_open_assistant_outputs(
            run.id,
            status=status,
            reason=reason,
        )
        if turn.summary_stream_id is not None:
            turn.summary_stream_id = None
            self.storage.save_run(run)

    def _close_turn_in_memory(
        self,
        run: RunRecord,
        outcome: Literal["answered", "succeeded", "failed", "cancelled"],
        summary: str,
        *,
        final_stream_id: str | None = None,
    ) -> dict[str, Any]:
        turn = self._active_turn(run)
        if turn.outcome != "in_progress":
            raise RuntimeError("The active turn was already closed")
        turn.outcome = outcome
        limit = 20_000 if outcome == "answered" else 4_000
        turn.summary = self._redact(summary)[:limit]
        turn.summary_stream_id = final_stream_id
        turn.completed_at = utc_now()
        self._scrub_provider_reasoning(run)
        payload: dict[str, Any] = {
            "index": turn.index,
            "outcome": outcome,
            "summary": turn.summary,
            "changed_files": turn.changed_files,
            "approval_mode": turn.approval_mode.value,
            "reasoning_effort": turn.reasoning_effort.value,
        }
        if final_stream_id:
            payload["final_stream_id"] = final_stream_id
        return payload

    async def _commit_terminal_turn(
        self,
        run: RunRecord,
        state: Literal[RunState.ANSWERED, RunState.FAILED, RunState.CANCELLED],
        outcome: Literal["answered", "failed", "cancelled"],
        summary: str,
        *,
        completion_payload: dict[str, Any],
        final_stream_id: str | None = None,
    ) -> bool:
        if run.state.terminal:
            raise RuntimeError("The terminal turn was already committed")
        self._validate_transition(run, state)
        if not await self._prepare_terminal_cleanup(run, state):
            await self._abandon_open_output_streams(
                run,
                status="interrupted",
                reason="terminal_cleanup_interrupted",
            )
            return False
        previous = run.state
        turn_payload = self._close_turn_in_memory(
            run,
            outcome,
            summary,
            final_stream_id=final_stream_id,
        )
        run.state = state
        terminal_events = self.storage.commit_terminal_turn(
            run,
            previous_state=previous,
            turn_payload=turn_payload,
            completion_payload=completion_payload,
        )
        await self._publish_persisted(terminal_events)
        return True

    def _reasoning_capability(self) -> ReasoningCapability:
        return resolve_reasoning_capability(self.settings.model, base_url=self.settings.base_url)

    @staticmethod
    def _scrub_provider_reasoning(run: RunRecord) -> bool:
        scrubbed = False
        for message in run.messages:
            if "reasoning_content" in message:
                message.pop("reasoning_content")
                scrubbed = True
        return scrubbed

    def _clarification_round(self, run_id: str) -> int:
        request_ids: set[str] = set()
        for event in reversed(self.storage.get_events(run_id)):
            if event.type is EventType.TURN_STARTED:
                break
            if event.type is EventType.CLARIFICATION_REQUESTED:
                request_id = event.payload.get("request_id")
                request_ids.add(str(request_id) if request_id else f"legacy-event:{event.seq}")
        return len(request_ids)

    @staticmethod
    def _pending_tool_call_id(run: RunRecord, expected_name: str) -> str | None:
        for index in range(len(run.messages) - 1, -1, -1):
            message = run.messages[index]
            if message.get("role") != "assistant":
                continue
            raw_calls = message.get("tool_calls")
            if not isinstance(raw_calls, list):
                continue
            completed_ids = {
                str(later.get("tool_call_id"))
                for later in run.messages[index + 1 :]
                if later.get("role") == "tool" and later.get("tool_call_id")
            }
            for raw_call in reversed(raw_calls):
                if not isinstance(raw_call, dict):
                    continue
                call_id = str(raw_call.get("id", ""))
                function = raw_call.get("function")
                if (
                    call_id
                    and call_id not in completed_ids
                    and isinstance(function, dict)
                    and function.get("name") == expected_name
                ):
                    return call_id
        return None

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
        assistant_index: int | None = None
        for index in range(len(run.messages) - 1, -1, -1):
            message = run.messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                assistant_index = index
                break
        if assistant_index is None:
            return 0
        batch = run.messages[assistant_index]["tool_calls"]
        completed_ids = {
            str(message.get("tool_call_id"))
            for message in run.messages[assistant_index + 1 :]
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        repaired = 0
        for call in batch:
            if str(call["id"]) in completed_ids:
                continue
            run.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["function"]["name"],
                    "content": boundary_safe_json_dumps(
                        {
                            "ok": False,
                            "error": "TraceForge stopped before this tool call completed.",
                        }
                    ),
                }
            )
            repaired += 1
        return repaired


def _model_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _append_stream_fragment(current: str, fragment: str) -> str:
    if not fragment or fragment == current:
        return current
    if fragment.startswith(current):
        return fragment
    return current + fragment


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
