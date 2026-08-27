export type RunState =
  | "created"
  | "planning"
  | "awaiting_clarification"
  | "awaiting_plan_approval"
  | "executing"
  | "awaiting_action_approval"
  | "verifying"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "rolled_back";

export type CheckStatus = "pending" | "running" | "passed" | "failed" | "waived";

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
  risks: string[];
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
  risk: "unknown" | "elevated" | "dangerous";
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
  verifier_enabled: boolean;
  plan: TaskPlan | null;
  clarification: ClarificationRequest | null;
  pending_approval: ApprovalRequest | null;
  verification: VerificationReport | null;
  step_count: number;
  repair_cycles: number;
  context_tokens: number;
  context_limit: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunEvent {
  run_id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface AppStatus {
  version: string;
  workspace: string;
  last_workspace: string;
  model: string;
  base_url: string;
  api_key_configured: boolean;
  suggested_task: string | null;
  limits: {
    context: number;
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
  api_key_configured: boolean;
  updated_at: string;
}

export interface ProviderProbe {
  ok: boolean;
  model: string;
  latency_ms: number;
  detail: string;
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

export interface RunTarget {
  project_id?: string;
  workspace?: string;
}

export interface ClarificationAnswer {
  question_id: string;
  option_id?: string;
  custom_text?: string;
}
