import { describe, expect, it } from "vitest";
import { mergeEvents, parseDiff, presentState } from "./lib";
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
});

