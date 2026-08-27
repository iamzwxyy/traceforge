from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunState(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    EXECUTING = "executing"
    AWAITING_ACTION_APPROVAL = "awaiting_action_approval"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ROLLED_BACK = "rolled_back"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELLED,
            self.ROLLED_BACK,
        }


class CheckStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class EventType(StrEnum):
    STATE_CHANGED = "state.changed"
    MESSAGE = "message"
    CLARIFICATION_REQUESTED = "clarification.requested"
    CLARIFICATION_ANSWERED = "clarification.answered"
    PLAN_UPDATED = "plan.updated"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    DIFF_UPDATED = "diff.updated"
    VERIFICATION_COMPLETED = "verification.completed"
    ERROR = "error"
    RUN_COMPLETED = "run.completed"
    ROLLBACK_COMPLETED = "rollback.completed"


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=400)
    recommended: bool = False


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=600)
    options: list[QuestionOption] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def validate_options(self) -> ClarificationQuestion:
        ids = [option.id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("Question option ids must be unique")
        if sum(option.recommended for option in self.options) > 1:
            raise ValueError("At most one option may be recommended")
        return self


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=3)
    round: int = Field(default=1, ge=1, le=2)


class ClarificationAnswer(BaseModel):
    question_id: str
    option_id: str | None = None
    custom_text: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def require_value(self) -> ClarificationAnswer:
        if not self.option_id and not self.custom_text:
            raise ValueError("An option or custom answer is required")
        return self


class AcceptanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=300)
    command: list[str] | None = None
    status: CheckStatus = CheckStatus.PENDING
    exit_code: int | None = None
    evidence: str = ""


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=1_000)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2_000)
    steps: list[PlanStep] = Field(min_length=1, max_length=12)
    acceptance_checks: list[AcceptanceCheck] = Field(min_length=1, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=10)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    name: str
    ok: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tool_call: ToolCall
    summary: str
    reason: str
    risk: Literal["unknown", "elevated", "dangerous"]


class VerificationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["critical", "high", "medium", "low"]
    title: str
    evidence: str
    suggested_fix: str = ""


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    summary: str
    findings: list[VerificationFinding] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int = Field(ge=1)
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task: str
    workspace: str
    state: RunState = RunState.CREATED
    verifier_enabled: bool = True
    plan: TaskPlan | None = None
    clarification: ClarificationRequest | None = None
    pending_approval: ApprovalRequest | None = None
    verification: VerificationReport | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    step_count: int = 0
    repair_cycles: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

