import { describe, expect, it } from "vitest";
import {
  buildActivityChapters,
  mergeEvents,
  modelRequestDetail,
  parseDiff,
  preferNewerRun,
  presentState,
  reasoningErrorLabel,
  shouldSubmitPrompt,
} from "./lib";
import type { Run, RunEvent } from "./types";

function event(seq: number): RunEvent {
  return { run_id: "run", seq, type: "message", payload: {}, created_at: "now" };
}

describe("mission control helpers", () => {
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

  it("submits prompts on Enter while preserving Shift+Enter and IME composition", () => {
    expect(shouldSubmitPrompt({ key: "Enter", shiftKey: false, isComposing: false })).toBe(true);
    expect(shouldSubmitPrompt({ key: "Enter", shiftKey: true, isComposing: false })).toBe(false);
    expect(shouldSubmitPrompt({ key: "Enter", shiftKey: false, isComposing: true })).toBe(false);
    expect(shouldSubmitPrompt({ key: "a", shiftKey: false, isComposing: false })).toBe(false);
  });

  it("describes disabled thinking before the omitted effort field", () => {
    expect(modelRequestDetail({ thinking: "disabled", omitted: true }, "none"))
      .toBe("已明确关闭思考模式");
    expect(modelRequestDetail({ thinking: "provider_default", omitted: true }, "auto"))
      .toBe("未发送强度字段，沿用模型默认");
    expect(modelRequestDetail({ wire_effort: "high", omitted: false }, "high"))
      .toBe("协议档位 high");
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
