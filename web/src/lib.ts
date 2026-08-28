import type { ProofPack, ReasoningEffort, Run, RunEvent, RunState } from "./types";

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

export function taskTitle(run: Pick<Run, "task">): string {
  const source = run.task.replace(/\s+/g, " ").trim();
  const sentenceEnd = source.search(/[。！？!?]/u);
  const sentence = sentenceEnd >= 0 ? source.slice(0, sentenceEnd + 1) : source;
  const characters = Array.from(sentence);
  return characters.length <= 48
    ? sentence
    : `${characters.slice(0, 47).join("")}…`;
}

export function mergeEvents(current: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const bySequence = new Map(current.map((event) => [event.seq, event]));
  for (const event of incoming) bySequence.set(event.seq, event);
  return [...bySequence.values()].sort((left, right) => left.seq - right.seq);
}

export function preferNewerRun(current: Run | null, incoming: Run): Run {
  if (!current || current.id !== incoming.id) return incoming;
  const preferred = incoming.updated_at >= current.updated_at ? incoming : current;
  const proofTurnIndexes = [...new Set([
    ...(current.proof_turn_indexes ?? []),
    ...(incoming.proof_turn_indexes ?? []),
  ])].sort((left, right) => left - right);
  const preferredIndexes = preferred.proof_turn_indexes ?? [];
  const parentRunId = current.parent_run_id ?? incoming.parent_run_id;
  const successorRunId = current.successor_run_id ?? incoming.successor_run_id;
  if (
    preferredIndexes.length === proofTurnIndexes.length
    && preferredIndexes.every((turnIndex, index) => turnIndex === proofTurnIndexes[index])
    && preferred.parent_run_id === parentRunId
    && preferred.successor_run_id === successorRunId
  ) return preferred;
  return {
    ...preferred,
    proof_turn_indexes: proofTurnIndexes,
    parent_run_id: parentRunId,
    successor_run_id: successorRunId,
  };
}

export function availableProofTurnIndexes(
  run: Pick<Run, "proof_turn_indexes">,
): number[] {
  return run.proof_turn_indexes;
}

export function proofPackTurnIndex(pack: Pick<ProofPack, "turn_index">): number {
  return pack.turn_index;
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

export function effectiveAssistantOutputStatus(
  status: string,
  runState: RunState,
  connected: boolean,
): string {
  if (["streaming", "provider_completed", "retrying", "reconnecting"].includes(status)) {
    if (runState === "interrupted") return "interrupted";
    if (runState === "cancelled") return "cancelled";
    if (runState === "failed") return "failed";
    if (runState === "rolled_back") return "discarded";
  }
  if (status === "streaming") return connected ? "streaming" : "reconnecting";
  return status;
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
  const projected: RunEvent[] = [];
  let turn = emptyConversationTurn();
  const flush = () => {
    if (turn.started) projected.push(turn.started);
    const finalStreamId = typeof turn.completed?.payload.final_stream_id === "string"
      ? turn.completed.payload.final_stream_id
      : null;
    const linked = finalStreamId ? turn.streams.get(finalStreamId) : undefined;
    if (turn.completed && linked) {
      projected.push(projectStream(linked, turn.completed));
    } else if (turn.completed) {
      const latest = latestStream(turn);
      if (latest && ["failed", "cancelled"].includes(
        String(turn.completed.payload.outcome ?? ""),
      )) {
        projected.push(projectStream(latest));
      }
      projected.push(turn.completed);
    } else {
      const latest = latestStream(turn);
      if (latest) projected.push(projectStream(latest));
      else if (turn.messages.length > 0) projected.push(turn.messages.at(-1)!);
    }
    turn = emptyConversationTurn();
  };

  for (const event of [...events].sort((left, right) => left.seq - right.seq)) {
    if (event.type === "turn.started") {
      flush();
      turn.started = event;
      const turnIndex = Number(event.payload.index ?? 0);
      turn.index = Number.isInteger(turnIndex) && turnIndex > 0 ? turnIndex : null;
      continue;
    }
    if (event.type === "message") {
      turn.messages.push(event);
      continue;
    }
    if (event.type === "assistant.output.started") {
      if (event.payload.surface !== "conversation") continue;
      if (!streamEventBelongsToTurn(event, turn)) continue;
      const streamId = String(event.payload.stream_id ?? "");
      if (!streamId || turn.streams.has(streamId)) continue;
      turn.streams.set(streamId, {
        started: event,
        segments: new Map(),
        status: "streaming",
        lastSeq: event.seq,
      });
      turn.streamOrder.push(streamId);
      continue;
    }
    if (event.type === "assistant.output.delta") {
      if (!streamEventBelongsToTurn(event, turn)) continue;
      const stream = turn.streams.get(String(event.payload.stream_id ?? ""));
      const segmentIndex = Number(event.payload.segment_index ?? 0);
      const delta = event.payload.delta;
      if (!stream || !Number.isInteger(segmentIndex) || segmentIndex < 1) continue;
      if (typeof delta === "string" && !stream.segments.has(segmentIndex)) {
        stream.segments.set(segmentIndex, delta);
      }
      stream.lastSeq = Math.max(stream.lastSeq, event.seq);
      continue;
    }
    if (event.type === "assistant.output.completed") {
      if (!streamEventBelongsToTurn(event, turn)) continue;
      const stream = turn.streams.get(String(event.payload.stream_id ?? ""));
      if (!stream) continue;
      if (typeof event.payload.content === "string") {
        stream.completedContent = event.payload.content;
      }
      stream.status = "provider_completed";
      stream.lastSeq = Math.max(stream.lastSeq, event.seq);
      continue;
    }
    if (event.type === "assistant.output.aborted") {
      if (!streamEventBelongsToTurn(event, turn)) continue;
      const stream = turn.streams.get(String(event.payload.stream_id ?? ""));
      if (!stream) continue;
      stream.status = String(event.payload.status ?? "failed");
      stream.reason = String(event.payload.reason ?? "stream_aborted");
      stream.lastSeq = Math.max(stream.lastSeq, event.seq);
      continue;
    }
    if (event.type !== "turn.completed") continue;
    const completedIndex = Number(event.payload.index ?? 0);
    if (
      turn.index === null
      || !Number.isInteger(completedIndex)
      || completedIndex !== turn.index
    ) continue;
    turn.completed = event;
    flush();
  }
  flush();

  return projected;
}

interface ConversationStream {
  started: RunEvent;
  segments: Map<number, string>;
  completedContent?: string;
  status: string;
  reason?: string;
  lastSeq: number;
}

interface ConversationTurnProjection {
  index: number | null;
  started: RunEvent | null;
  messages: RunEvent[];
  streams: Map<string, ConversationStream>;
  streamOrder: string[];
  completed: RunEvent | null;
}

function emptyConversationTurn(): ConversationTurnProjection {
  return {
    index: null,
    started: null,
    messages: [],
    streams: new Map(),
    streamOrder: [],
    completed: null,
  };
}

function streamEventBelongsToTurn(
  event: RunEvent,
  turn: ConversationTurnProjection,
): boolean {
  const eventTurnIndex = Number(event.payload.turn_index ?? 0);
  return turn.index !== null
    && Number.isInteger(eventTurnIndex)
    && eventTurnIndex === turn.index;
}

function latestStream(turn: ConversationTurnProjection): ConversationStream | undefined {
  const streamId = turn.streamOrder.at(-1);
  return streamId ? turn.streams.get(streamId) : undefined;
}

function projectStream(stream: ConversationStream, terminal?: RunEvent): RunEvent {
  const content = terminal
    ? String(terminal.payload.summary ?? "")
    : stream.completedContent ?? [...stream.segments.entries()]
      .sort(([left], [right]) => left - right)
      .map(([, delta]) => delta)
      .join("");
  return {
    ...stream.started,
    type: "assistant.output",
    payload: {
      ...stream.started.payload,
      content,
      status: terminal ? "committed" : stream.status,
      reason: stream.reason,
      ...(terminal?.payload ?? {}),
      terminal_seq: terminal?.seq,
      stream_last_seq: stream.lastSeq,
    },
  };
}

export function projectProgressEvents(events: RunEvent[]): RunEvent[] {
  const progress: RunEvent[] = [];
  let messages: RunEvent[] = [];
  const flush = (summary?: string) => {
    progress.push(...messages.filter((event) =>
      summary === undefined || String(event.payload.content ?? "") !== summary
    ));
    messages = [];
  };

  for (const event of events) {
    if (event.type === "turn.started") {
      flush();
    } else if (event.type === "message") {
      messages.push(event);
    } else if (event.type === "turn.completed") {
      flush(String(event.payload.summary ?? ""));
    }
  }
  flush();
  return progress;
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
