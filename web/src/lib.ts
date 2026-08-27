import type { RunEvent, RunState } from "./types";

export interface StatePresentation {
  label: string;
  tone: "idle" | "active" | "waiting" | "success" | "danger";
}

const states: Record<RunState, StatePresentation> = {
  created: { label: "Created", tone: "idle" },
  planning: { label: "Planning", tone: "active" },
  awaiting_clarification: { label: "Needs input", tone: "waiting" },
  awaiting_plan_approval: { label: "Plan review", tone: "waiting" },
  executing: { label: "Building", tone: "active" },
  awaiting_action_approval: { label: "Approval", tone: "waiting" },
  verifying: { label: "Verifying", tone: "active" },
  succeeded: { label: "Proven", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Stopped", tone: "danger" },
  interrupted: { label: "Interrupted", tone: "waiting" },
  rolled_back: { label: "Rolled back", tone: "idle" },
};

export function presentState(state: RunState): StatePresentation {
  return states[state];
}

export function mergeEvents(current: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const bySequence = new Map(current.map((event) => [event.seq, event]));
  for (const event of incoming) bySequence.set(event.seq, event);
  return [...bySequence.values()].sort((left, right) => left.seq - right.seq);
}

export interface DiffLine {
  kind: "header" | "hunk" | "add" | "remove" | "context";
  text: string;
}

export function parseDiff(diff: string): DiffLine[] {
  return diff.split("\n").map((text) => {
    if (text.startsWith("+++ ") || text.startsWith("--- ")) return { kind: "header", text };
    if (text.startsWith("@@")) return { kind: "hunk", text };
    if (text.startsWith("+")) return { kind: "add", text };
    if (text.startsWith("-")) return { kind: "remove", text };
    return { kind: "context", text };
  });
}

export function isActiveState(state: RunState): boolean {
  return !["succeeded", "failed", "cancelled", "interrupted", "rolled_back"].includes(state);
}

