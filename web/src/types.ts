export type RunState =
  | "created"
  | "planning"
  | "awaiting_clarification"
  | "awaiting_plan_approval"
  | "executing"
  | "awaiting_action_approval"
  | "verifying"
  | "answered"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "rolled_back";

export type CheckStatus = "pending" | "running" | "passed" | "failed" | "waived";
export type InteractionMode = "agent" | "plan";
export type DecisionKind = "clarification" | "plan" | "action";
export type ApprovalMode = "manual" | "automatic" | "full_access";
export type ReasoningEffort =
  | "auto"
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max";

export interface QuestionOption {
  id: string;
  label: string;
  description: string;
  recommended: boolean;
}

export interface ClarificationQuestion {
  id: string;
  prompt: string;
  options: QuestionOption[];
}

export interface ClarificationRequest {
  questions: ClarificationQuestion[];
  round: number;
  purpose: "requirements" | "project_scope";
}

export interface ProjectCandidate {
  id: string;
  path: string;
  label: string;
  description: string;
  markers: string[];
  identity: string;
}

export interface ProjectScope {
  path: string;
  label: string;
  markers: string[];
  selected_by: "automatic" | "explicit" | "clarification" | "inherited";
  identity: string;
  root_listed: boolean;
  evidence_read: string[];
}

export interface AcceptanceCheck {
  id: string;
  label: string;
  command: string[] | null;
  status: CheckStatus;
  exit_code: number | null;
  evidence: string;
}

export interface PlanStep {
  id: string;
  title: string;
  description: string;
  status: "pending" | "in_progress" | "completed";
}

export interface TaskPlan {
  summary: string;
  steps: PlanStep[];
  acceptance_checks: AcceptanceCheck[];
  impacted_files: string[];
  risks: string[];
  approach: string;
  markdown: string;
}

export interface PlanGate {
  decision: "auto_approved" | "approval_required" | "agent_continues";
  risk: "low" | "medium" | "high";
  reasons: string[];
  assessed_at: string;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ApprovalRequest {
  id: string;
  tool_call: ToolCall;
  summary: string;
  reason: string;
  risk: "low" | "unknown" | "elevated" | "dangerous";
  approval_mode: ApprovalMode;
  policy_decision: "allow" | "ask" | "deny";
  sandbox_bypass_on_approve: boolean;
}

export interface VerificationFinding {
  severity: "critical" | "high" | "medium" | "low";
  title: string;
  evidence: string;
  suggested_fix: string;
}

export interface VerificationReport {
  verdict: "pass" | "fail" | "inconclusive";
  summary: string;
  findings: VerificationFinding[];
  checked_at: string;
}

export interface Run {
  id: string;
  task: string;
  workspace: string;
  project_id: string | null;
  state: RunState;
  mode: InteractionMode;
  approval_mode: ApprovalMode;
  reasoning_effort: ReasoningEffort;
  turns: ConversationTurn[];
  proof_turn_indexes: number[];
  decision_request_id: string | null;
  decision_kind: DecisionKind | null;
  parent_run_id: string | null;
  successor_run_id: string | null;
  verifier_enabled: boolean;
  plan: TaskPlan | null;
  clarification: ClarificationRequest | null;
  pending_approval: ApprovalRequest | null;
  verification: VerificationReport | null;
  plan_gate: PlanGate | null;
  step_count: number;
  repair_cycles: number;
  context_tokens: number;
  context_limit: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationTurn {
  index: number;
  request: string;
  mode: InteractionMode;
  approval_mode: ApprovalMode;
  reasoning_effort: ReasoningEffort;
  outcome: "in_progress" | "answered" | "succeeded" | "failed" | "cancelled";
  summary: string;
  summary_stream_id: string | null;
  changed_files: string[];
  project_candidates: ProjectCandidate[];
  project_scope: ProjectScope | null;
  started_at: string;
  completed_at: string | null;
}

export interface RunEvent {
  run_id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface WorkspaceInstructionReference {
  path: string;
  scope: string;
  content_sha256: string;
  byte_count: number;
}

export interface WorkspaceInstructionManifest {
  schema_version: "traceforge.workspace-instructions.v1";
  captured_at: string;
  sources: WorkspaceInstructionReference[];
  total_bytes: number;
  snapshot_sha256: string;
  turn_index: number;
  status: "loaded";
  authority: "guidance";
  content_private: true;
}

export interface RollbackResult {
  restored: string[];
  removed: string[];
  conflicts: string[];
}

export interface AppStatus {
  version: string;
  workspace: string;
  last_workspace: string;
  model: string;
  base_url: string;
  api_key_configured: boolean;
  connection_verified: boolean;
  suggested_task: string | null;
  mode: "demo" | "standard";
  sandbox: {
    backend: "seatbelt" | "bubblewrap" | "none";
    enforced: boolean;
    detail: string;
  };
  limits: {
    context: number;
    context_source: "configured" | "catalog" | "fallback";
    steps: number;
    repair_cycles: number;
  };
}

export interface Project {
  id: string;
  name: string;
  root: string;
  created_at: string;
  updated_at: string;
  last_opened_at: string;
}

export interface ProviderConfig {
  model: string;
  base_url: string | null;
  credential_source: "file" | "environment" | "missing";
  credential_file: string | null;
  credential_env: string;
  environment_credential_configured: boolean;
  api_key_configured: boolean;
  connection_verified: boolean;
  verified_at: string | null;
  context_window: number | null;
  resolved_context_window: number;
  context_window_source: "configured" | "catalog" | "fallback";
  supported_reasoning_efforts: ReasoningEffort[];
  default_reasoning_effort: ReasoningEffort | null;
  reasoning_effort_source: "openai_catalog" | "deepseek_catalog" | "provider_default";
  reasoning_effort_catalog_version: string;
  updated_at: string;
}

export interface ProviderUpdate {
  model: string;
  base_url: string | null;
  credential_file: string | null;
  context_window: number | null;
  api_key?: string;
}

export interface ProviderProbe {
  ok: boolean;
  model: string;
  latency_ms: number;
  detail: string;
  provider: ProviderConfig;
}

export interface DirectoryEntry {
  name: string;
  path: string;
}

export interface DirectoryListing {
  current: string;
  parent: string | null;
  children: DirectoryEntry[];
}

export interface DirectoryChoice {
  supported: boolean;
  path: string | null;
}

export interface OpenWorkspaceResult {
  supported: boolean;
  opened: boolean;
  application: "Finder" | "file_manager" | null;
}

export interface RunTarget {
  project_id?: string;
  workspace?: string;
  create_direct_workspace?: boolean;
}

export interface ProofRollback {
  status: "available" | "completed" | "not_available";
  conflict_aware: boolean;
  restored: string[];
  removed: string[];
  conflicts: string[];
}

export interface ProofPack {
  schema_version: "traceforge.proof-pack.v2";
  generated_at: string;
  run_id: string;
  turn_index: number;
  scope: "cumulative_through_turn";
  event_through_seq: number;
  task: string;
  workspace: string;
  project_id: string | null;
  mode: InteractionMode;
  turns: ConversationTurn[];
  state: RunState;
  proof_status: "in_progress" | "proven" | "checks_only" | "not_proven";
  plan: TaskPlan | null;
  plan_gate: PlanGate | null;
  changed_files: string[];
  diff: string;
  diff_source: "completion_event" | "diff_event" | "live_workspace";
  diff_sha256: string;
  checks_fresh: boolean;
  verification: VerificationReport | null;
  rollback: ProofRollback;
  command_sandbox: {
    status: "enforced" | "mixed" | "bypassed" | "policy_only" | "not_used";
    backends: string[];
    sandboxed_commands: number;
    bypassed_commands: number;
    policy_only_commands: number;
    not_executed_commands: number;
  };
  event_count: number;
  event_chain_sha256: string;
  step_count: number;
  repair_cycles: number;
  created_at: string;
  updated_at: string;
  evidence_sha256: string;
  artifact_sha256: string;
}

export interface ClarificationAnswer {
  question_id: string;
  option_id?: string;
  custom_text?: string;
}
