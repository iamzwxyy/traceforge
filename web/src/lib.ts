import type { ReasoningEffort, Run, RunEvent, RunState } from "./types";

export interface StatePresentation {
  label: string;
  tone: "idle" | "active" | "waiting" | "success" | "danger";
}

const states: Record<RunState, StatePresentation> = {
  created: { label: "已创建", tone: "idle" },
  planning: { label: "正在规划", tone: "active" },
  awaiting_clarification: { label: "等待补充", tone: "waiting" },
  awaiting_plan_approval: { label: "等待审批", tone: "waiting" },
  executing: { label: "正在执行", tone: "active" },
  awaiting_action_approval: { label: "动作审批", tone: "waiting" },
  verifying: { label: "正在验证", tone: "active" },
  answered: { label: "已答复", tone: "success" },
  succeeded: { label: "已证实", tone: "success" },
  failed: { label: "失败", tone: "danger" },
  cancelled: { label: "已停止", tone: "danger" },
  interrupted: { label: "已中断", tone: "waiting" },
  rolled_back: { label: "已回滚", tone: "idle" },
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
  return ![
    "answered", "succeeded", "failed", "cancelled", "interrupted", "rolled_back",
  ].includes(state);
}

export function shouldSubmitPrompt(event: {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
}): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing;
}

export function supportedReasoningEffort(
  efforts: ReasoningEffort[],
  requested: ReasoningEffort,
): ReasoningEffort {
  if (efforts.includes(requested)) return requested;
  if (efforts.includes("auto")) return "auto";
  return efforts[0] ?? "auto";
}

export function projectConversationEvents(events: RunEvent[]): RunEvent[] {
  const conversation = events.filter((event) =>
    ["turn.started", "turn.completed", "message"].includes(event.type)
  );
  const hidden = new Set<number>();
  let turnStart = -1;

  for (const [index, event] of conversation.entries()) {
    if (event.type === "turn.started") {
      turnStart = index;
      continue;
    }
    if (event.type !== "turn.completed") continue;
    if (["answered", "succeeded"].includes(String(event.payload.outcome ?? ""))) {
      const summary = String(event.payload.summary ?? "");
      for (let candidate = index - 1; candidate > turnStart; candidate -= 1) {
        const previous = conversation[candidate];
        if (previous.type !== "message") continue;
        if (
          ["planning", "building", "verifying"].includes(
            String(previous.payload.phase ?? ""),
          )
          && String(previous.payload.content ?? "") === summary
        ) {
          hidden.add(previous.seq);
        }
      }
    }
    turnStart = -1;
  }

  return conversation.filter((event) => !hidden.has(event.seq));
}

export function reasoningErrorLabel(message: string): string | null {
  if (message.startsWith("Provider-private replay state was removed")) {
    return "模型私有推理已从当前记录清除，但 SQLite WAL 正被外部读取占用。请关闭外部数据库读取程序，然后停止或继续此任务。";
  }
  if (message.startsWith("Provider-private cleanup is still waiting")) {
    return "SQLite WAL 仍被外部读取占用；请先关闭外部数据库读取程序，再继续此任务。";
  }
  if (message.startsWith("The paused turn's reasoning effort is incompatible")) {
    return "中断轮次的思考强度与当前精确模型路由不兼容；请恢复兼容的模型设置后再继续。";
  }
  if (message.startsWith("Reasoning effort '")) {
    return "当前精确模型路由不支持所选思考强度；请改选页面提供的档位。";
  }
  return null;
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
  if (firstEvent.type === "run.resumed") return "恢复 · 已安全继续";
  if (firstEvent.type === "repair.started") {
    return `修复轮次 ${String(firstEvent.payload.cycle ?? occurrence)}`;
  }
  if (phase === "planning") return "规划与决策";
  if (phase === "building") return occurrence === 1 ? "执行与检查" : `修复轮次 ${occurrence - 1}`;
  return occurrence === 1 ? "完成后复核" : `复核轮次 ${occurrence}`;
}
