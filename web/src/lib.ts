import type {
  AmbiguityDimension,
  ClarificationRequest,
  ProofPack,
  ProjectScope,
  ProjectTarget,
  ReasoningEffort,
  RequestResolution,
  Run,
  RunEvent,
  RunState,
  WorkspaceInstructionManifest,
  WorkspaceInstructionReference,
} from "./types";

export interface ClarificationPresentation {
  eyebrow: string;
  title: string;
  detail: string;
  submitLabel: string;
  allowsCustomAnswer: boolean;
}

export function clarificationPresentation(
  purpose: ClarificationRequest["purpose"],
): ClarificationPresentation {
  if (purpose === "project_scope") {
    return {
      eyebrow: "选择项目",
      title: "选择本轮的目标项目",
      detail: "选择只确定本轮目标，不授予执行权限。读取、写入和命令都受该项目边界约束；执行仍按当前审批与沙箱策略处理。",
      submitLabel: "确认目标项目",
      allowsCustomAnswer: false,
    };
  }
  return {
    eyebrow: "需求澄清",
    title: "确认会实质影响结果的选择",
    detail: "问题只涉及仍会改变目标、范围、约束或验收的选择。需求澄清最多两轮，项目目标选择不占用该额度。",
    submitLabel: "继续",
    allowsCustomAnswer: true,
  };
}

export function currentProjectTarget(
  run: Pick<Run, "turns">,
): ProjectTarget | null {
  const turn = run.turns.at(-1);
  return turn?.project_target ?? turn?.project_scope ?? null;
}

export function currentRequestResolution(
  run: Pick<Run, "turns">,
): RequestResolution | null {
  return run.turns.at(-1)?.request_resolution ?? null;
}

export interface RequestResolutionPresentation {
  workKind: string;
  targetStatus: string;
  ambiguityDimensions: string[];
}

const ambiguityDimensionLabels: Record<AmbiguityDimension, string> = {
  target: "目标",
  scope: "范围",
  constraint: "约束",
  acceptance: "验收",
};

export function requestResolutionPresentation(
  resolution: RequestResolution,
): RequestResolutionPresentation {
  const workKind = {
    conversation: "对话",
    read: "只读",
    execute: "执行",
    undetermined: "待判定",
  }[resolution.work_kind];
  const targetStatus = {
    not_required: "无需项目目标",
    resolved: "目标已确定",
    clarification_required: "需要选择项目",
    unsupported: "当前目标范围不受支持",
  }[resolution.target_status];
  return {
    workKind,
    targetStatus,
    ambiguityDimensions: resolution.ambiguity_dimensions.map(
      (dimension) => ambiguityDimensionLabels[dimension],
    ),
  };
}

export function currentProjectScope(
  run: Pick<Run, "turns">,
): ProjectScope | null {
  return run.turns.at(-1)?.project_scope ?? null;
}

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

export type ProviderPreset = "openai" | "deepseek" | "custom";

const providerModels: Record<Exclude<ProviderPreset, "custom">, readonly string[]> = {
  openai: [
    "gpt-5.6-sol",
    "gpt-5.6",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.3-codex",
    "gpt-5",
  ],
  deepseek: [
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
  ],
};
const providerDefaults: Record<Exclude<ProviderPreset, "custom">, string> = {
  openai: "gpt-5.6-sol",
  deepseek: "deepseek-v4-flash-vision-exp",
};

function matchesOfficialEndpoint(raw: string, hostname: string): boolean {
  const trimmed = raw.trim();
  const parsed = trimmed.match(
    /^([a-z][a-z0-9+.-]*):\/\/([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/iu,
  );
  if (!parsed) return false;
  const scheme = parsed[1] ?? "";
  const authority = parsed[2] ?? "";
  const rawPath = parsed[3] ?? "";
  const query = parsed[4] ?? "";
  const fragment = parsed[5] ?? "";
  const normalizedAuthority = authority.toLocaleLowerCase("en-US");
  const path = rawPath.replace(/\/+$/u, "");
  return scheme.toLocaleLowerCase("en-US") === "https"
    && normalizedAuthority === hostname
    && (path === "" || path === "/v1")
    && !query
    && !fragment;
}

export function inferProviderPreset(baseUrl: string | null): ProviderPreset {
  if (!baseUrl?.trim()) return "openai";
  if (matchesOfficialEndpoint(baseUrl, "api.openai.com")) return "openai";
  if (matchesOfficialEndpoint(baseUrl, "api.deepseek.com")) return "deepseek";
  return "custom";
}

export function providerModelSuggestions(preset: ProviderPreset): readonly string[] {
  return preset === "custom" ? [] : providerModels[preset];
}

export function providerPresetValues(
  preset: ProviderPreset,
  currentModel: string,
  currentBaseUrl: string,
): { model: string; baseUrl: string } {
  if (preset === "custom") {
    return {
      model: currentModel,
      baseUrl: inferProviderPreset(currentBaseUrl) === "custom" ? currentBaseUrl : "",
    };
  }
  const suggestions = providerModelSuggestions(preset);
  const normalized = currentModel.trim().toLocaleLowerCase("en-US");
  const model = suggestions.find((candidate) => candidate === normalized)
    ?? providerDefaults[preset];
  return {
    model,
    baseUrl: preset === "openai" ? "" : "https://api.deepseek.com",
  };
}

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

export function backgroundRunRefreshDelay(
  runs: readonly Pick<Run, "id" | "state">[],
  selectedRunId: string | null,
): number | null {
  const backgroundRuns = runs.filter((run) => run.id !== selectedRunId);
  if (backgroundRuns.some((run) => [
    "created", "planning", "executing", "verifying",
  ].includes(run.state))) {
    return 2_000;
  }
  return backgroundRuns.some((run) => isActiveState(run.state)) ? 15_000 : null;
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

export function workspaceInstructionManifest(
  event: RunEvent,
): WorkspaceInstructionManifest | null {
  if (event.type !== "workspace.instructions.resolved") return null;
  const payload = event.payload;
  const turnIndex = Number(payload.turn_index);
  const totalBytes = Number(payload.total_bytes);
  if (
    payload.schema_version !== "traceforge.workspace-instructions.v1"
    || payload.status !== "loaded"
    || payload.authority !== "guidance"
    || payload.content_private !== true
    || typeof payload.captured_at !== "string"
    || !Number.isInteger(turnIndex)
    || turnIndex < 1
    || !Number.isInteger(totalBytes)
    || totalBytes < 0
    || typeof payload.snapshot_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(payload.snapshot_sha256)
    || !Array.isArray(payload.sources)
    || payload.sources.length !== 1
  ) return null;
  const sources: WorkspaceInstructionReference[] = [];
  for (const rawSource of payload.sources) {
    if (typeof rawSource !== "object" || rawSource === null || Array.isArray(rawSource)) {
      return null;
    }
    const source = rawSource as Record<string, unknown>;
    const byteCount = Number(source.byte_count);
    if (
      source.path !== "AGENTS.md"
      || source.scope !== "."
      || typeof source.content_sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(source.content_sha256)
      || !Number.isInteger(byteCount)
      || byteCount < 0
    ) return null;
    sources.push({
      path: source.path,
      scope: source.scope,
      content_sha256: source.content_sha256,
      byte_count: byteCount,
    });
  }
  if (sources.reduce((total, source) => total + source.byte_count, 0) !== totalBytes) {
    return null;
  }
  return {
    schema_version: "traceforge.workspace-instructions.v1",
    captured_at: payload.captured_at,
    sources,
    total_bytes: totalBytes,
    snapshot_sha256: payload.snapshot_sha256,
    turn_index: turnIndex,
    status: "loaded",
    authority: "guidance",
    content_private: true,
  };
}

export function latestWorkspaceInstructionManifest(
  events: RunEvent[],
  currentTurnIndex: number,
): WorkspaceInstructionManifest | null {
  let latest: { seq: number; manifest: WorkspaceInstructionManifest } | null = null;
  for (const event of events) {
    const manifest = workspaceInstructionManifest(event);
    if (
      manifest?.turn_index === currentTurnIndex
      && (latest === null || event.seq > latest.seq)
    ) latest = { seq: event.seq, manifest };
  }
  return latest?.manifest ?? null;
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

export function workspaceInstructionErrorLabel(message: string): string | null {
  const exact: Record<string, string> = {
    "The workspace root has too many entries to inspect safely.": "工作区根目录超过 10,000 个直接条目，无法安全检查 AGENTS.md。",
    "The workspace root could not be inspected for AGENTS.md.": "无法检查工作区根目录中的 AGENTS.md。",
    "The root AGENTS.md metadata could not be read safely.": "无法安全读取根目录 AGENTS.md 的元数据。",
    "The root AGENTS.md path could not be resolved safely.": "无法安全解析根目录 AGENTS.md 的路径。",
    "The root AGENTS.md must resolve directly inside the workspace.": "根目录 AGENTS.md 必须直接解析到当前工作区内。",
    "The root AGENTS.md changed while it was being opened.": "根目录 AGENTS.md 在打开期间发生变化，已拒绝载入。",
    "The root AGENTS.md changed while it was being read.": "根目录 AGENTS.md 在读取期间发生变化，已拒绝载入。",
    "The root AGENTS.md could not be read safely.": "无法安全读取根目录 AGENTS.md。",
    "The stored AGENTS.md snapshot conflicts with the current credential and cannot be sent to the model.": "已保存的 AGENTS.md 快照与当前凭证冲突，已阻止发送给模型。请恢复原凭证，或停止后开始新一轮。",
    "Protected model context and tool schemas exceed the configured context window": "工作区规则、当前请求和工具说明无法共同放入当前模型的上下文窗口；请缩短 AGENTS.md 或调整已确认的上下文容量。",
    "Model request contains credential-like data and was blocked before provider transmission": "模型请求与当前凭证冲突，已在发送前阻止。请检查工作区规则或恢复原凭证。",
    "Model request contains credential-like data and was blocked before HTTP transmission": "模型请求与当前凭证冲突，已在网络发送前阻止。请检查工作区规则或恢复原凭证。",
  };
  if (exact[message]) return exact[message];
  if (message.startsWith("This legacy turn has no immutable workspace-instruction snapshot")) {
    return "此旧版中断轮次没有不可变工作区规则快照，不能安全恢复；请先停止，再继续对话，或新建任务。";
  }
  if (message.startsWith("This task's stored context conflicts with the current provider credential")) {
    if (message.includes("stop this interrupted turn")) {
      return "此任务的已保存上下文与当前模型凭证冲突，不能发送给模型；请恢复原凭证，或先停止本轮再开始新任务。";
    }
    return "此任务的已保存上下文与当前模型凭证冲突，不能发送给模型；请恢复原凭证，或新建任务。";
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
  "workspace.instructions.resolved",
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
