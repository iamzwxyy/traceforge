import { describe, expect, it } from "vitest";
import { buildActivityChapters, mergeEvents, parseDiff, presentState } from "./lib";
import type { RunEvent } from "./types";

function event(seq: number): RunEvent {
  return { run_id: "run", seq, type: "message", payload: {}, created_at: "now" };
}

describe("mission control helpers", () => {
  it("deduplicates and orders replayed events", () => {
    expect(mergeEvents([event(2)], [event(1), event(2)]).map((item) => item.seq)).toEqual([
      1, 2,
    ]);
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
    expect(presentState("succeeded")).toEqual({ label: "Proven", tone: "success" });
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
      "Planning & decisions",
      "Build & checks",
      "Independent verification",
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

    expect(chapters.at(-1)?.label).toBe("Repair cycle 1");
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
      "Build & checks",
      "Recovery · resumed safely",
    ]);
  });
});
