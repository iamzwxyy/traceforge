import { describe, expect, it } from "vitest";
import {
  availableProofTurnIndexes,
  buildActivityChapters,
  effectiveAssistantOutputStatus,
  inferProviderPreset,
  mergeEvents,
  parseDiff,
  preferNewerRun,
  presentState,
  proofPackTurnIndex,
  providerModelSuggestions,
  providerPresetValues,
  projectConversationEvents,
  projectProgressEvents,
  reasoningErrorLabel,
  shouldSubmitPrompt,
  supportedReasoningEffort,
  taskTitle,
} from "./lib";
import type { Run, RunEvent } from "./types";

function event(seq: number): RunEvent {
  return { run_id: "run", seq, type: "message", payload: {}, created_at: "now" };
}

describe("mission control helpers", () => {
  it("infers official provider presets with the same fail-closed route boundary", () => {
    expect(inferProviderPreset(null)).toBe("openai");
    expect(inferProviderPreset("  ")).toBe("openai");
    expect(inferProviderPreset("https://api.openai.com/v1/")).toBe("openai");
    expect(inferProviderPreset("https://api.openai.com:/v1")).toBe("custom");
    expect(inferProviderPreset("https://api.deepseek.com")).toBe("deepseek");
    expect(inferProviderPreset("https://api.deepseek.com/v1////")).toBe("deepseek");
    expect(inferProviderPreset("https://api.openai.com/v1/.")).toBe("custom");
    expect(inferProviderPreset("http://api.openai.com/v1")).toBe("custom");
    expect(inferProviderPreset("https://api.openai.com:443/v1")).toBe("custom");
    expect(inferProviderPreset("https://api.openai.com.evil.example/v1")).toBe("custom");
    expect(inferProviderPreset("https://user@api.deepseek.com/v1")).toBe("custom");
    expect(inferProviderPreset("https://api.deepseek.com/v1/models")).toBe("custom");
    expect(inferProviderPreset("https://api.openai.com:bad/v1")).toBe("custom");
  });

  it("pairs official presets with a safe endpoint and known model default", () => {
    expect(providerPresetValues("openai", "gpt-5.4", "https://old.example/v1"))
      .toEqual({ model: "gpt-5.4", baseUrl: "" });
    expect(providerPresetValues("openai", "unknown-model", "https://old.example/v1"))
      .toEqual({ model: "gpt-5.6-sol", baseUrl: "" });
    expect(providerPresetValues("deepseek", "unknown-model", ""))
      .toEqual({
        model: "deepseek-v4-flash-vision-exp",
        baseUrl: "https://api.deepseek.com",
      });
    expect(providerPresetValues("custom", " local-model ", "http://localhost:11434/v1"))
      .toEqual({ model: " local-model ", baseUrl: "http://localhost:11434/v1" });
    expect(providerPresetValues("custom", "gpt-5.6-sol", ""))
      .toEqual({ model: "gpt-5.6-sol", baseUrl: "" });
    expect(providerPresetValues("custom", "gpt-5.6-sol", "https://api.openai.com/v1"))
      .toEqual({ model: "gpt-5.6-sol", baseUrl: "" });
  });

  it("suggests only models with an exact built-in provider capability route", () => {
    expect(providerModelSuggestions("openai")).toEqual([
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
    ]);
    expect(providerModelSuggestions("deepseek")).toEqual([
      "deepseek-v4-flash-vision-exp",
      "deepseek-v4-flash",
      "deepseek-v4-pro",
    ]);
    expect(providerModelSuggestions("custom")).toEqual([]);
  });

  it("deduplicates and orders replayed events", () => {
    expect(mergeEvents([event(2)], [event(1), event(2)]).map((item) => item.seq)).toEqual([
      1, 2,
    ]);
  });

  it("does not let a delayed metadata response regress the selected run", () => {
    const newer = { id: "run", state: "succeeded", updated_at: "2026-08-27T12:00:00.200Z" } as Run;
    const stale = { id: "run", state: "executing", updated_at: "2026-08-27T12:00:00.100Z" } as Run;

    expect(preferNewerRun(newer, stale)).toBe(newer);
    expect(preferNewerRun(stale, newer)).toBe(newer);
  });

  it("keeps immutable Proof availability when equal-timestamp metadata arrives late", () => {
    const withProof = {
      id: "run",
      state: "succeeded",
      updated_at: "2026-08-27T12:00:00.200Z",
      proof_turn_indexes: [1],
    } as unknown as Run;
    const withoutProof = {
      ...withProof,
      proof_turn_indexes: [],
    };

    expect(preferNewerRun(withProof, withoutProof).proof_turn_indexes).toEqual([1]);
  });

  it("keeps immutable rollback lineage when equal-timestamp metadata arrives late", () => {
    const linked = {
      id: "parent",
      state: "rolled_back",
      updated_at: "2026-08-27T12:00:00.200Z",
      parent_run_id: null,
      successor_run_id: "successor",
      proof_turn_indexes: [],
    } as unknown as Run;
    const stale = {
      ...linked,
      successor_run_id: null,
    };

    expect(preferNewerRun(linked, stale).successor_run_id).toBe("successor");
    expect(preferNewerRun(stale, linked).successor_run_id).toBe("successor");
  });

  it("uses frozen Proof availability instead of successful turn outcomes", () => {
    expect(availableProofTurnIndexes({ proof_turn_indexes: [1, 3] })).toEqual([1, 3]);
    expect(availableProofTurnIndexes({ proof_turn_indexes: [] })).toEqual([]);
    expect(proofPackTurnIndex({ turn_index: 3 })).toBe(3);
  });

  it("classifies unified diff lines", () => {
    const lines = parseDiff("--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new");
    expect(lines.map((line) => line.kind)).toEqual([
      "header",
      "header",
      "hunk",
      "remove",
      "add",
    ]);
  });

  it("presents evidence-backed success distinctly", () => {
    expect(presentState("succeeded")).toEqual({ label: "已证实", tone: "success" });
    expect(presentState("answered")).toEqual({ label: "已答复", tone: "success" });
  });

  it("settles provisional output labels when the run terminates", () => {
    expect(effectiveAssistantOutputStatus("retrying", "interrupted", true))
      .toBe("interrupted");
    expect(effectiveAssistantOutputStatus("provider_completed", "failed", true))
      .toBe("failed");
    expect(effectiveAssistantOutputStatus("retrying", "rolled_back", true))
      .toBe("discarded");
    expect(effectiveAssistantOutputStatus("streaming", "cancelled", false))
      .toBe("cancelled");
    expect(effectiveAssistantOutputStatus("retrying", "cancelled", true))
      .toBe("cancelled");
    expect(effectiveAssistantOutputStatus("streaming", "executing", false))
      .toBe("reconnecting");
    expect(effectiveAssistantOutputStatus("committed", "rolled_back", false))
      .toBe("committed");
  });

  it("submits prompts on Enter while preserving Shift+Enter and IME composition", () => {
    expect(shouldSubmitPrompt({ key: "Enter", shiftKey: false, isComposing: false })).toBe(true);
    expect(shouldSubmitPrompt({ key: "Enter", shiftKey: true, isComposing: false })).toBe(false);
    expect(shouldSubmitPrompt({ key: "Enter", shiftKey: false, isComposing: true })).toBe(false);
    expect(shouldSubmitPrompt({ key: "a", shiftKey: false, isComposing: false })).toBe(false);
  });

  it("falls back only to a capability the exact model actually supports", () => {
    expect(supportedReasoningEffort(["none"], "high")).toBe("none");
    expect(supportedReasoningEffort(["low", "high"], "max")).toBe("low");
    expect(supportedReasoningEffort(["high", "auto"], "max")).toBe("auto");
    expect(supportedReasoningEffort([], "high")).toBe("auto");
  });

  it("keeps completed turns conversation-first and moves distinct progress to Trace", () => {
    const events = [
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      { ...event(2), payload: { phase: "planning", content: "Inspecting first." } },
      { ...event(3), payload: { phase: "planning", content: "Final answer" } },
      { ...event(4), payload: { phase: "planning", content: "Distinct later progress" } },
      {
        ...event(5),
        type: "turn.completed",
        payload: { index: 1, outcome: "answered", summary: "Final answer" },
      },
    ];
    const projected = projectConversationEvents([
      ...events,
    ]);

    expect(projected.map((item) => item.seq)).toEqual([1, 5]);
    expect(projectProgressEvents(events).map((item) => item.seq)).toEqual([2, 4]);
  });

  it("keeps one live progress update while retaining every update for Trace", () => {
    const events = [
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      { ...event(2), payload: { phase: "planning", content: "First progress" } },
      { ...event(3), payload: { phase: "building", content: "Latest progress" } },
    ];

    expect(projectConversationEvents(events).map((item) => item.seq))
      .toEqual([1, 3]);
    expect(projectProgressEvents(events).map((item) => item.seq)).toEqual([2, 3]);
  });

  it("commits a streamed answer into one stable canonical conversation item", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "stream-1", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "stream-1", turn_index: 1, segment_index: 1, delta: "Hello ",
        },
      },
      {
        ...event(4),
        type: "assistant.output.completed",
        payload: { stream_id: "stream-1", turn_index: 1, content: "Hello world" },
      },
      {
        ...event(5),
        type: "turn.completed",
        payload: {
          index: 1,
          outcome: "answered",
          summary: "Hello world",
          final_stream_id: "stream-1",
        },
      },
    ]);

    expect(projected.map((item) => item.type)).toEqual([
      "turn.started",
      "assistant.output",
    ]);
    expect(projected[1].payload).toMatchObject({
      stream_id: "stream-1",
      content: "Hello world",
      status: "committed",
      outcome: "answered",
      terminal_seq: 5,
    });
  });

  it("replays streamed segments by identity without duplicates or ordering drift", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "stream-1", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(4),
        type: "assistant.output.delta",
        payload: {
          stream_id: "stream-1", turn_index: 1, segment_index: 2, delta: "world",
        },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "stream-1", turn_index: 1, segment_index: 1, delta: "Hello ",
        },
      },
      {
        ...event(5),
        type: "assistant.output.delta",
        payload: {
          stream_id: "stream-1", turn_index: 1, segment_index: 1, delta: "duplicate",
        },
      },
    ]);

    expect(projected.at(-1)?.payload).toMatchObject({
      content: "Hello world",
      status: "streaming",
    });
  });

  it("replaces a retrying partial with the newer isolated stream attempt", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "first", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "first", turn_index: 1, segment_index: 1, delta: "old partial",
        },
      },
      {
        ...event(4),
        type: "assistant.output.aborted",
        payload: {
          stream_id: "first", turn_index: 1, status: "retrying", reason: "connection",
        },
      },
      {
        ...event(5),
        type: "assistant.output.started",
        payload: { stream_id: "second", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(6),
        type: "assistant.output.delta",
        payload: {
          stream_id: "second", turn_index: 1, segment_index: 1, delta: "new partial",
        },
      },
    ]);

    expect(projected).toHaveLength(2);
    expect(projected[1].payload).toMatchObject({
      stream_id: "second",
      content: "new partial",
      status: "streaming",
    });
  });

  it("preserves an interrupted partial beside an accurate failed terminal result", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "partial", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "partial", turn_index: 1, segment_index: 1, delta: "unfinished",
        },
      },
      {
        ...event(4),
        type: "assistant.output.aborted",
        payload: {
          stream_id: "partial", turn_index: 1, status: "failed", reason: "protocol",
        },
      },
      {
        ...event(5),
        type: "turn.completed",
        payload: { index: 1, outcome: "failed", summary: "Provider protocol failed" },
      },
    ]);

    expect(projected.map((item) => item.type)).toEqual([
      "turn.started",
      "assistant.output",
      "turn.completed",
    ]);
    expect(projected[1].payload).toMatchObject({
      content: "unfinished",
      status: "failed",
    });
  });

  it("ignores streamed events whose turn identity does not match the active bucket", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "active-stream", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "active-stream", turn_index: 1, segment_index: 1, delta: "safe",
        },
      },
      {
        ...event(4),
        type: "assistant.output.delta",
        payload: {
          stream_id: "active-stream", turn_index: 2, segment_index: 2, delta: " leak",
        },
      },
      {
        ...event(5),
        type: "assistant.output.completed",
        payload: { stream_id: "active-stream", turn_index: 2, content: "leak complete" },
      },
      {
        ...event(6),
        type: "assistant.output.aborted",
        payload: { stream_id: "active-stream", turn_index: 2, status: "failed" },
      },
    ]);

    expect(projected.map((item) => item.type)).toEqual(["turn.started", "assistant.output"]);
    expect(projected.at(-1)?.payload).toMatchObject({ content: "safe", status: "streaming" });
  });

  it("ignores a terminal event whose turn identity does not match the active bucket", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "stream-1", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "stream-1", turn_index: 1, segment_index: 1, delta: "Canonical",
        },
      },
      {
        ...event(4),
        type: "turn.completed",
        payload: { index: 2, outcome: "answered", summary: "Wrong turn" },
      },
      {
        ...event(5),
        type: "turn.completed",
        payload: {
          index: 1,
          outcome: "answered",
          summary: "Canonical",
          final_stream_id: "stream-1",
        },
      },
    ]);

    expect(projected.map((item) => item.seq)).toEqual([1, 2]);
    expect(projected.at(-1)?.payload).toMatchObject({
      content: "Canonical",
      status: "committed",
      terminal_seq: 5,
    });
  });

  it("preserves the cancelled draft through terminal and rollback event ordering", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "cancelled-draft", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "cancelled-draft", turn_index: 1, segment_index: 1, delta: "Draft",
        },
      },
      {
        ...event(4),
        type: "assistant.output.completed",
        payload: { stream_id: "cancelled-draft", turn_index: 1, content: "Draft" },
      },
      {
        ...event(5),
        type: "assistant.output.aborted",
        payload: { stream_id: "cancelled-draft", turn_index: 1, status: "cancelled" },
      },
      { ...event(6), type: "state.changed", payload: { state: "cancelled" } },
      {
        ...event(7),
        type: "turn.completed",
        payload: { index: 1, outcome: "cancelled", summary: "The user stopped this turn." },
      },
      { ...event(8), type: "rollback.completed", payload: { removed: [], restored: [] } },
    ]);

    expect(projected.map((item) => item.type)).toEqual([
      "turn.started",
      "assistant.output",
      "turn.completed",
    ]);
    expect(projected[1].payload).toMatchObject({ content: "Draft", status: "cancelled" });
  });

  it("settles a retry-backoff draft when cancellation closes the turn", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      {
        ...event(2),
        type: "assistant.output.started",
        payload: { stream_id: "retry-draft", turn_index: 1, surface: "conversation" },
      },
      {
        ...event(3),
        type: "assistant.output.delta",
        payload: {
          stream_id: "retry-draft", turn_index: 1, segment_index: 1, delta: "Partial",
        },
      },
      {
        ...event(4),
        type: "assistant.output.aborted",
        payload: { stream_id: "retry-draft", turn_index: 1, status: "retrying" },
      },
      { ...event(5), type: "state.changed", payload: { state: "cancelled" } },
      {
        ...event(6),
        type: "turn.completed",
        payload: { index: 1, outcome: "cancelled", summary: "The user stopped this turn." },
      },
    ]);
    const draft = projected.find((item) => item.type === "assistant.output");

    expect(draft?.payload).toMatchObject({ content: "Partial", status: "retrying" });
    expect(effectiveAssistantOutputStatus(String(draft?.payload.status), "cancelled", true))
      .toBe("cancelled");
  });

  it("keeps one canonical response per completed turn without cross-turn folding", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      { ...event(2), payload: { phase: "planning", content: "Progress" } },
      {
        ...event(3),
        type: "turn.completed",
        payload: { index: 1, outcome: "answered", summary: "Same answer" },
      },
      { ...event(4), type: "turn.started", payload: { index: 2 } },
      {
        ...event(5),
        type: "turn.completed",
        payload: { index: 2, outcome: "answered", summary: "Same answer" },
      },
    ]);

    expect(projected.map((item) => item.seq)).toEqual([1, 3, 4, 5]);
  });

  it("hides an exact legacy builder draft before a successful terminal summary", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      { ...event(2), payload: { phase: "building", content: "Implemented and tested" } },
      {
        ...event(3),
        type: "turn.completed",
        payload: { index: 1, outcome: "succeeded", summary: "Implemented and tested" },
      },
    ]);

    expect(projected.map((item) => item.seq)).toEqual([1, 3]);
  });

  it("derives a stable concise task title without hiding the original request", () => {
    expect(taskTitle({
      task: "修复多租户缓存隔离问题，不能改变 TTL 或缓存命中行为。补充回归测试，并证明完整测试套件通过。",
    })).toBe("修复多租户缓存隔离问题，不能改变 TTL 或缓存命中行为。");
    const plannedRun = {
      task: "  Inspect the cache key\nwithout changing TTL  ",
      plan: { summary: "A plan that must not rename the task" } as Run["plan"],
    };
    expect(taskTitle(plannedRun)).toBe("Inspect the cache key without changing TTL");
    expect(Array.from(taskTitle({ task: "🙂".repeat(60) }))).toHaveLength(48);
  });

  it("keeps the latest progress for an incomplete legacy or interrupted turn", () => {
    const projected = projectConversationEvents([
      { ...event(1), type: "turn.started", payload: { index: 1 } },
      { ...event(2), payload: { phase: "planning", content: "Earlier" } },
      { ...event(3), payload: { phase: "building", content: "Last durable progress" } },
    ]);

    expect(projected.map((item) => item.seq)).toEqual([1, 3]);
  });

  it("turns reasoning capability failures into actionable Chinese guidance", () => {
    expect(reasoningErrorLabel(
      "Reasoning effort 'max' is not supported by this exact model route; choose one of: auto",
    )).toBe("当前精确模型路由不支持所选思考强度；请改选页面提供的档位。");
    expect(reasoningErrorLabel(
      "The paused turn's reasoning effort is incompatible with the current exact model route.",
    )).toBe("中断轮次的思考强度与当前精确模型路由不兼容；请恢复兼容的模型设置后再继续。");
    expect(reasoningErrorLabel(
      "Provider-private cleanup is still waiting for an external SQLite reader.",
    )).toBe("SQLite WAL 仍被外部读取占用；请先关闭外部数据库读取程序，再继续此任务。");
    expect(reasoningErrorLabel(
      "Provider-private replay state was removed from the active record, but cleanup is pending.",
    )).toBe("模型私有推理已从当前记录清除，但 SQLite WAL 正被外部读取占用。请关闭外部数据库读取程序，然后停止或继续此任务。");
    expect(reasoningErrorLabel("Model request timed out")).toBeNull();
  });

  it("groups persisted activity into progressive-disclosure chapters", () => {
    const events: RunEvent[] = [
      event(1),
      { ...event(2), type: "state.changed", payload: { state: "executing" } },
      { ...event(3), type: "tool.completed" },
      { ...event(4), type: "state.changed", payload: { state: "verifying" } },
      { ...event(5), type: "verification.completed" },
    ];

    const chapters = buildActivityChapters(events);

    expect(chapters.map((chapter) => chapter.label)).toEqual([
      "规划与决策",
      "执行与检查",
      "完成后复核",
    ]);
    expect(chapters.map((chapter) => chapter.events.map((item) => item.seq))).toEqual([
      [1],
      [3],
      [5],
    ]);
  });

  it("makes post-verifier execution a distinct repair chapter", () => {
    const chapters = buildActivityChapters([
      { ...event(1), type: "state.changed", payload: { state: "executing" } },
      { ...event(2), type: "tool.completed" },
      { ...event(3), type: "state.changed", payload: { state: "verifying" } },
      { ...event(4), type: "verification.completed" },
      { ...event(5), type: "repair.started", payload: { cycle: 1 } },
      { ...event(6), type: "state.changed", payload: { state: "executing" } },
      { ...event(7), type: "tool.completed" },
    ]);

    expect(chapters.at(-1)?.label).toBe("修复轮次 1");
    expect(chapters.at(-1)?.events[0].type).toBe("repair.started");
  });

  it("starts a separate recovery chapter after an interrupted execution", () => {
    const chapters = buildActivityChapters([
      { ...event(1), type: "state.changed", payload: { state: "executing" } },
      { ...event(2), type: "tool.completed" },
      { ...event(3), type: "state.changed", payload: { state: "interrupted" } },
      {
        ...event(4),
        type: "run.resumed",
        payload: { strategy: "inspect_before_execution" },
      },
      { ...event(5), type: "state.changed", payload: { state: "executing" } },
      { ...event(6), type: "tool.completed" },
    ]);

    expect(chapters.map((chapter) => chapter.label)).toEqual([
      "执行与检查",
      "恢复 · 已安全继续",
    ]);
  });
});
