import type {
  AppStatus,
  ClarificationAnswer,
  Run,
  RunEvent,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({ detail: response.statusText }))) as {
      detail?: string;
    };
    throw new Error(body.detail || `Request failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  status: () => request<AppStatus>("/api/status"),
  listRuns: () => request<Run[]>("/api/runs"),
  getRun: (runId: string) => request<Run>(`/api/runs/${runId}`),
  getEvents: (runId: string, afterSeq = 0) =>
    request<RunEvent[]>(`/api/runs/${runId}/events?after_seq=${afterSeq}`),
  getDiff: (runId: string) => request<{ diff: string }>(`/api/runs/${runId}/diff`),
  createRun: (task: string, verifierEnabled: boolean) =>
    request<Run>("/api/runs", {
      method: "POST",
      body: JSON.stringify({ task, verifier_enabled: verifierEnabled }),
    }),
  answerQuestions: (runId: string, answers: ClarificationAnswer[]) =>
    request<{ accepted: boolean }>(`/api/runs/${runId}/answers`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    }),
  decidePlan: (runId: string, decision: "approve" | "revise", feedback = "") =>
    request<{ accepted: boolean }>(`/api/runs/${runId}/plan-decision`, {
      method: "POST",
      body: JSON.stringify({ decision, feedback }),
    }),
  decideAction: (runId: string, approvalId: string, approved: boolean) =>
    request<{ accepted: boolean }>(
      `/api/runs/${runId}/actions/${approvalId}/decision`,
      { method: "POST", body: JSON.stringify({ approved }) },
    ),
  cancel: (runId: string) => request<Run>(`/api/runs/${runId}/cancel`, { method: "POST" }),
  resume: (runId: string) => request<Run>(`/api/runs/${runId}/resume`, { method: "POST" }),
  rollback: (runId: string) =>
    request<{ restored: string[]; removed: string[]; conflicts: string[] }>(
      `/api/runs/${runId}/rollback`,
      { method: "POST" },
    ),
};

