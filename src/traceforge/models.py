from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
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
    ANSWERED = "answered"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ROLLED_BACK = "rolled_back"

    @property
    def terminal(self) -> bool:
        return self in {
            self.ANSWERED,
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELLED,
            self.ROLLED_BACK,
        }


class InteractionMode(StrEnum):
    AGENT = "agent"
    PLAN = "plan"


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    FULL_ACCESS = "full_access"


class ReasoningEffort(StrEnum):
    AUTO = "auto"
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


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
    PLAN_GATED = "plan.gated"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    DIFF_UPDATED = "diff.updated"
    VERIFICATION_COMPLETED = "verification.completed"
    REPAIR_STARTED = "repair.started"
    MODEL_REQUESTED = "model.requested"
    MODEL_RETRY = "model.retry"
    RUN_RESUMED = "run.resumed"
    ERROR = "error"
    RUN_COMPLETED = "run.completed"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
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


class DirectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20_000)


class FinishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=4_000)


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
    impacted_files: list[str] = Field(default_factory=list, max_length=24)
    risks: list[str] = Field(default_factory=list, max_length=10)
    approach: str = Field(default="", max_length=4_000)
    markdown: str = Field(default="", max_length=20_000)

    @model_validator(mode="after")
    def validate_scope(self) -> TaskPlan:
        normalized: list[str] = []
        for raw in self.impacted_files:
            path = PurePosixPath(raw.strip())
            if (
                not raw.strip()
                or path.is_absolute()
                or path.as_posix() == "."
                or ".." in path.parts
                or ".git" in path.parts
            ):
                raise ValueError(f"Impacted file must be a safe relative path: {raw}")
            normalized.append(path.as_posix())
        if len(normalized) != len(set(normalized)):
            raise ValueError("Impacted files must be unique")
        self.impacted_files = normalized
        # The structured contract is authoritative. Materialize one canonical document so the
        # downloadable plan cannot drift from the scope and checks enforced by the runtime.
        self.markdown = _plan_markdown(self)
        return self


class PlanGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["auto_approved", "approval_required", "agent_continues"]
    risk: Literal["low", "medium", "high"]
    reasons: list[str] = Field(min_length=1, max_length=12)
    assessed_at: datetime = Field(default_factory=utc_now)


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
    risk: Literal["low", "unknown", "elevated", "dangerous"]
    approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC
    policy_decision: Literal["allow", "ask", "deny"] = "ask"
    sandbox_bypass_on_approve: bool = False


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


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=120)
    root: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_opened_at: datetime = Field(default_factory=utc_now)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2_000)
    credential_file: str | None = Field(default=None, max_length=4_096)
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    updated_at: datetime = Field(default_factory=utc_now)


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    request: str = Field(min_length=1, max_length=20_000)
    mode: InteractionMode = InteractionMode.AGENT
    approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC
    reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO
    outcome: Literal["in_progress", "answered", "succeeded", "failed", "cancelled"] = (
        "in_progress"
    )
    summary: str = Field(default="", max_length=20_000)
    changed_files: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class ProofRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "completed", "not_available"]
    conflict_aware: bool = True
    restored: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class ProofCommandSandbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["enforced", "mixed", "bypassed", "policy_only", "not_used"]
    backends: list[str] = Field(default_factory=list)
    sandboxed_commands: int = Field(ge=0)
    bypassed_commands: int = Field(ge=0)
    policy_only_commands: int = Field(ge=0)
    not_executed_commands: int = Field(default=0, ge=0)


class ProofPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["traceforge.proof-pack.v1"] = "traceforge.proof-pack.v1"
    generated_at: datetime = Field(default_factory=utc_now)
    run_id: str
    task: str
    workspace: str
    project_id: str | None = None
    mode: InteractionMode
    turns: list[ConversationTurn] = Field(default_factory=list)
    state: RunState
    proof_status: Literal["in_progress", "proven", "checks_only", "not_proven"]
    plan: TaskPlan | None = None
    plan_gate: PlanGate | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""
    diff_source: Literal["completion_event", "diff_event", "live_workspace"]
    diff_sha256: str
    checks_fresh: bool
    verification: VerificationReport | None = None
    rollback: ProofRollback
    command_sandbox: ProofCommandSandbox
    event_count: int = Field(ge=0)
    event_chain_sha256: str
    step_count: int = Field(ge=0)
    repair_cycles: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    evidence_sha256: str


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task: str
    workspace: str
    project_id: str | None = None
    state: RunState = RunState.CREATED
    mode: InteractionMode = InteractionMode.AGENT
    approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC
    reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO
    turns: list[ConversationTurn] = Field(default_factory=list)
    verifier_enabled: bool = True
    plan: TaskPlan | None = None
    clarification: ClarificationRequest | None = None
    pending_approval: ApprovalRequest | None = None
    verification: VerificationReport | None = None
    plan_gate: PlanGate | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    provider_reasoning_cleanup_pending: bool = False
    plan_approved: bool = False
    interrupted_from: RunState | None = None
    step_count: int = 0
    repair_cycles: int = 0
    context_limit: int = Field(default=64_000, ge=1, le=10_000_000)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


def _plan_markdown(plan: TaskPlan) -> str:
    chinese = any("\u3400" <= character <= "\u9fff" for character in plan.summary)
    labels = (
        {
            "title": "实施计划",
            "goal": "目标",
            "approach": "方案",
            "steps": "步骤",
            "changes": "预期变更",
            "validation": "验收",
            "risks": "风险",
            "unknown_scope": "实施时确认文件范围。",
            "no_risks": "未识别到常规回归风险之外的重大风险。",
        }
        if chinese
        else {
            "title": "Implementation plan",
            "goal": "Goal",
            "approach": "Approach",
            "steps": "Steps",
            "changes": "Expected changes",
            "validation": "Validation",
            "risks": "Risks",
            "unknown_scope": "File scope will be confirmed during implementation.",
            "no_risks": "No material risks identified beyond normal regression risk.",
        }
    )
    lines = [f"# {labels['title']}", "", f"## {labels['goal']}", "", plan.summary]
    if plan.approach.strip():
        lines.extend(["", f"## {labels['approach']}", "", plan.approach.strip()])
    lines.extend(["", f"## {labels['steps']}", ""])
    for step in plan.steps:
        detail = f" — {step.description}" if step.description else ""
        lines.append(f"- [ ] **{step.title}**{detail}")
    lines.extend(["", f"## {labels['changes']}", ""])
    if plan.impacted_files:
        lines.extend(f"- `{path}`" for path in plan.impacted_files)
    else:
        lines.append(f"- {labels['unknown_scope']}")
    lines.extend(["", f"## {labels['validation']}", ""])
    for check in plan.acceptance_checks:
        command = f" — `{' '.join(check.command)}`" if check.command else ""
        lines.append(f"- [ ] {check.label}{command}")
    lines.extend(["", f"## {labels['risks']}", ""])
    lines.extend(f"- {risk}" for risk in plan.risks)
    if not plan.risks:
        lines.append(f"- {labels['no_risks']}")
    return "\n".join(lines)
