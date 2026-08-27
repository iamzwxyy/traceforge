import type { Run, RunEvent, RunState } from "./types";

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

export function preferNewerRun(current: Run | null, incoming: Run): Run {
  if (!current || current.id !== incoming.id) return incoming;
  return incoming.updated_at >= current.updated_at ? incoming : current;
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

export type ActivityPhase = "planning" | "building" | "verifying";

export interface ActivityChapter {
  id: string;
  phase: ActivityPhase;
  label: string;
  events: RunEvent[];
}

const activityEventTypes = new Set([
  "message",
  "tool.completed",
  "plan.gated",
  "verification.completed",
  "repair.started",
  "model.retry",
  "run.resumed",
  "error",
]);

export function buildActivityChapters(events: RunEvent[]): ActivityChapter[] {
  const chapters: ActivityChapter[] = [];
  const occurrences: Record<ActivityPhase, number> = {
    planning: 0,
    building: 0,
    verifying: 0,
  };
  let phase: ActivityPhase = "planning";
  let current: ActivityChapter | null = null;

  for (const event of events) {
    if (event.type === "state.changed") {
      const next = phaseForState(String(event.payload.state ?? ""));
      if (next && next !== phase) {
        phase = next;
        current = null;
      }
      continue;
    }
    if (event.type === "run.resumed") {
      const strategy = String(event.payload.strategy ?? "");
      phase = strategy.includes("planning") || strategy.includes("clarification")
        || strategy.includes("plan_approval") ? "planning" : "building";
      current = null;
    } else if (event.type === "repair.started") {
      phase = "building";
      current = null;
    }
    if (!activityEventTypes.has(event.type)) continue;
    if (!current) {
      occurrences[phase] += 1;
      current = {
        id: `${phase}-${event.seq}`,
        phase,
        label: chapterLabel(phase, occurrences[phase], event),
        events: [],
      };
      chapters.push(current);
    }
    current.events.push(event);
  }
  return chapters;
}

function phaseForState(state: string): ActivityPhase | null {
  if (["created", "planning", "awaiting_clarification", "awaiting_plan_approval"].includes(state)) {
    return "planning";
  }
  if (["executing", "awaiting_action_approval"].includes(state)) return "building";
  if (state === "verifying") return "verifying";
  return null;
}

function chapterLabel(phase: ActivityPhase, occurrence: number, firstEvent: RunEvent): string {
  if (firstEvent.type === "run.resumed") return "Recovery · resumed safely";
  if (firstEvent.type === "repair.started") {
    return `Repair cycle ${String(firstEvent.payload.cycle ?? occurrence)}`;
  }
  if (phase === "planning") return "Planning & decisions";
  if (phase === "building") return occurrence === 1 ? "Build & checks" : `Repair cycle ${occurrence - 1}`;
  return occurrence === 1 ? "Independent verification" : `Verification round ${occurrence}`;
}
