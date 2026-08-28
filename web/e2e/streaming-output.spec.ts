import { expect, test, type Page } from "@playwright/test";
import { expectNoWcagViolations } from "./a11y";

const createdAt = "2026-08-28T00:00:00Z";

function json(body: unknown) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function activeRun() {
  return {
    id: "stream-run",
    task: "Stream one trustworthy answer",
    workspace: "/tmp/stream-run",
    project_id: null,
    state: "planning",
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: "auto",
    proof_turn_indexes: [],
    decision_request_id: null,
    decision_kind: null,
    parent_run_id: null,
    successor_run_id: null,
    turns: [{
      index: 1,
      request: "Stream one trustworthy answer",
      mode: "agent",
      approval_mode: "automatic",
      reasoning_effort: "auto",
      outcome: "in_progress",
      summary: "",
      summary_stream_id: null,
      changed_files: [],
      started_at: createdAt,
      completed_at: null,
    }],
    verifier_enabled: true,
    plan: null,
    clarification: null,
    pending_approval: null,
    verification: null,
    plan_gate: null,
    step_count: 0,
    repair_cycles: 0,
    context_tokens: 100,
    context_limit: 64_000,
    error: null,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

async function installSocketHarness(page: Page) {
  await page.addInitScript(() => {
    class MockWebSocket {
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      private closed = false;

      constructor(readonly url: string | URL) {
        sockets.push(this);
        queueMicrotask(() => this.onopen?.(new Event("open")));
      }

      close() {
        if (this.closed) return;
        this.closed = true;
        this.onclose?.(new CloseEvent("close"));
      }

      send() {}
    }

    const sockets: MockWebSocket[] = [];
    const harness = globalThis as typeof globalThis & {
      __traceforgeEmitLatest?: (data: unknown) => void;
      __traceforgeEmitSocket?: (index: number, data: unknown) => void;
      __traceforgeCloseLatest?: () => void;
      __traceforgeSocketCount?: () => number;
      __traceforgeSocketUrls?: () => string[];
    };
    harness.__traceforgeEmitLatest = (data) => {
      sockets.at(-1)?.onmessage?.(
        new MessageEvent("message", { data: JSON.stringify(data) }),
      );
    };
    harness.__traceforgeEmitSocket = (index, data) => {
      sockets[index]?.onmessage?.(
        new MessageEvent("message", { data: JSON.stringify(data) }),
      );
    };
    harness.__traceforgeCloseLatest = () => sockets.at(-1)?.close();
    harness.__traceforgeSocketCount = () => sockets.length;
    harness.__traceforgeSocketUrls = () => sockets.map((socket) => String(socket.url));
    Object.defineProperty(globalThis, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
    });
  });
}

async function emit(page: Page, event: unknown) {
  await page.evaluate((payload) => {
    (globalThis as typeof globalThis & {
      __traceforgeEmitLatest?: (data: unknown) => void;
    }).__traceforgeEmitLatest?.(payload);
  }, event);
}

async function emitSocket(page: Page, index: number, event: unknown) {
  await page.evaluate(({ socketIndex, payload }) => {
    (globalThis as typeof globalThis & {
      __traceforgeEmitSocket?: (index: number, data: unknown) => void;
    }).__traceforgeEmitSocket?.(socketIndex, payload);
  }, { socketIndex: index, payload: event });
}

async function mockApp(
  page: Page,
  run: ReturnType<typeof activeRun>,
  events: unknown[],
  options: { failDiff?: boolean } = {},
) {
  let runGets = 0;
  await page.route("**/api/status", (route) => route.fulfill(json({
    version: "test",
    workspace: run.workspace,
    last_workspace: run.workspace,
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    api_key_configured: true,
    connection_verified: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64_000, context_source: "catalog", steps: 30, repair_cycles: 2 },
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([run])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json({
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    credential_source: "environment",
    credential_file: null,
    credential_env: "OPENAI_API_KEY",
    api_key_configured: true,
    connection_verified: true,
    verified_at: createdAt,
    context_window: null,
    resolved_context_window: 64_000,
    context_window_source: "catalog",
    supported_reasoning_efforts: ["auto"],
    default_reasoning_effort: null,
    reasoning_effort_source: "provider_default",
    reasoning_effort_catalog_version: "test",
    updated_at: createdAt,
  })));
  await page.route("**/api/runs/stream-run", (route) => {
    runGets += 1;
    return route.fulfill(json(run));
  });
  await page.route("**/api/runs/stream-run/events?*", (route) => route.fulfill(json(events)));
  await page.route("**/api/runs/stream-run/diff", (route) => (
    options.failDiff
      ? route.fulfill({ status: 503, contentType: "application/json", body: "{}" })
      : route.fulfill(json({ diff: "" }))
  ));
  return () => runGets;
}

test("streamed output stays one provisional bubble and commits without duplication", async ({ page }) => {
  await installSocketHarness(page);
  const run = activeRun();
  const getRunGets = await mockApp(page, run, [{
    run_id: run.id,
    seq: 1,
    type: "turn.started",
    payload: {
      index: 1,
      request: run.task,
      approval_mode: "automatic",
      reasoning_effort: "auto",
    },
    created_at: createdAt,
  }]);
  await page.goto("/");
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeSocketCount?: () => number;
    }).__traceforgeSocketCount?.() ?? 0
  ))).toBe(1);
  const baselineRunGets = getRunGets();

  await emit(page, {
    run_id: run.id,
    seq: 2,
    type: "assistant.output.started",
    payload: { stream_id: "answer-1", turn_index: 1, surface: "conversation" },
    created_at: createdAt,
  });
  await emit(page, {
    run_id: run.id,
    seq: 3,
    type: "assistant.output.delta",
    payload: { stream_id: "answer-1", turn_index: 1, segment_index: 1, delta: "Hello " },
    created_at: createdAt,
  });
  const bubble = page.locator(".assistant-turn.streamed-output");
  await expect(bubble).toHaveCount(1);
  await expect(bubble).toContainText("Hello");
  await expect(bubble).toHaveAttribute("aria-busy", "true");

  await emit(page, {
    run_id: run.id,
    seq: 4,
    type: "assistant.output.delta",
    payload: { stream_id: "answer-1", turn_index: 1, segment_index: 2, delta: "world" },
    created_at: createdAt,
  });
  await expect(bubble).toContainText("Hello world");
  await emit(page, {
    run_id: run.id,
    seq: 4,
    type: "assistant.output.delta",
    payload: { stream_id: "answer-1", turn_index: 1, segment_index: 2, delta: "world" },
    created_at: createdAt,
  });
  await expect(page.getByText("Hello world", { exact: true })).toHaveCount(1);
  expect(getRunGets()).toBe(baselineRunGets);

  await emit(page, {
    run_id: run.id,
    seq: 5,
    type: "assistant.output.completed",
    payload: {
      stream_id: "answer-1",
      turn_index: 1,
      content: "Hello world",
      status: "provider_completed",
    },
    created_at: createdAt,
  });
  await expect(bubble).toContainText("等待提交");
  await expect(bubble).toHaveAttribute("aria-busy", "false");

  run.state = "answered";
  run.turns[0].outcome = "answered";
  run.turns[0].summary = "Hello world";
  run.turns[0].summary_stream_id = "answer-1";
  run.turns[0].completed_at = createdAt;
  await emit(page, {
    run_id: run.id,
    seq: 6,
    type: "turn.completed",
    payload: {
      index: 1,
      outcome: "answered",
      summary: "Hello world",
      changed_files: [],
      final_stream_id: "answer-1",
    },
    created_at: createdAt,
  });

  await expect(bubble).toHaveCount(1);
  await expect(bubble).toContainText("正式结果");
  await expect(page.getByText("Hello world", { exact: true })).toHaveCount(1);
  await expectNoWcagViolations(page, "committed streamed output");
});

test("stream reconnect resumes after the durable sequence and ignores the old socket", async ({ page }) => {
  await installSocketHarness(page);
  const run = activeRun();
  await mockApp(page, run, [{
    run_id: run.id,
    seq: 1,
    type: "turn.started",
    payload: { index: 1, request: run.task },
    created_at: createdAt,
  }]);
  await page.goto("/");
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeSocketCount?: () => number;
    }).__traceforgeSocketCount?.() ?? 0
  ))).toBe(1);

  await emit(page, {
    run_id: run.id,
    seq: 2,
    type: "assistant.output.started",
    payload: { stream_id: "reconnect-answer", turn_index: 1, surface: "conversation" },
    created_at: createdAt,
  });
  await emit(page, {
    run_id: run.id,
    seq: 3,
    type: "assistant.output.delta",
    payload: {
      stream_id: "reconnect-answer",
      turn_index: 1,
      segment_index: 1,
      delta: "Durable ",
    },
    created_at: createdAt,
  });
  const bubble = page.locator(".assistant-turn.streamed-output");
  await expect(bubble).toContainText("Durable");

  await page.evaluate(() => {
    (globalThis as typeof globalThis & {
      __traceforgeCloseLatest?: () => void;
    }).__traceforgeCloseLatest?.();
  });
  await expect(bubble).toContainText("正在重连");
  await expect(page.locator(".activity-feed .sr-only[role=status]"))
    .toContainText("TraceForge 正在重连");
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeSocketCount?: () => number;
    }).__traceforgeSocketCount?.() ?? 0
  ))).toBe(2);
  const urls = await page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeSocketUrls?: () => string[];
    }).__traceforgeSocketUrls?.() ?? []
  ));
  expect(urls[1]).toContain("after_seq=3");

  await emitSocket(page, 0, {
    run_id: run.id,
    seq: 4,
    type: "assistant.output.delta",
    payload: {
      stream_id: "reconnect-answer",
      turn_index: 1,
      segment_index: 2,
      delta: "stale",
    },
    created_at: createdAt,
  });
  await expect(bubble).not.toContainText("stale");
  await emitSocket(page, 1, {
    run_id: run.id,
    seq: 4,
    type: "assistant.output.delta",
    payload: {
      stream_id: "reconnect-answer",
      turn_index: 1,
      segment_index: 2,
      delta: "continuation",
    },
    created_at: createdAt,
  });
  await expect(bubble).toContainText("Durable continuation");
  await expect(page.getByText("Durable continuation", { exact: true })).toHaveCount(1);
});

test("new stream content does not steal scroll from a user reading history", async ({ page }) => {
  await installSocketHarness(page);
  const run = activeRun();
  const events: Record<string, unknown>[] = [];
  for (let index = 1; index <= 14; index += 1) {
    events.push({
      run_id: run.id,
      seq: index * 2 - 1,
      type: "turn.started",
      payload: { index, request: `Historical request ${index}` },
      created_at: createdAt,
    });
    events.push({
      run_id: run.id,
      seq: index * 2,
      type: "turn.completed",
      payload: { index, outcome: "answered", summary: `Historical answer ${index}` },
      created_at: createdAt,
    });
  }
  events.push({
    run_id: run.id,
    seq: 29,
    type: "turn.started",
    payload: { index: 15, request: run.task },
    created_at: createdAt,
  });
  await mockApp(page, run, events);
  await page.setViewportSize({ width: 390, height: 640 });
  await page.goto("/");
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeSocketCount?: () => number;
    }).__traceforgeSocketCount?.() ?? 0
  ))).toBe(1);
  const feed = page.locator(".activity-feed");
  await feed.evaluate((element) => {
    element.scrollTop = 0;
    element.dispatchEvent(new Event("scroll"));
  });

  await emit(page, {
    run_id: run.id,
    seq: 30,
    type: "assistant.output.started",
    payload: { stream_id: "answer-15", turn_index: 15, surface: "conversation" },
    created_at: createdAt,
  });
  await emit(page, {
    run_id: run.id,
    seq: 31,
    type: "assistant.output.delta",
    payload: {
      stream_id: "answer-15",
      turn_index: 15,
      segment_index: 1,
      delta: `Long streamed URL https://example.com/${"segment".repeat(40)}`,
    },
    created_at: createdAt,
  });

  await expect.poll(() => feed.evaluate((element) => element.scrollTop)).toBe(0);
  const jump = page.getByRole("button", { name: "有新内容" });
  await expect(jump).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);
  await jump.click();
  await expect.poll(() => feed.evaluate((element) => (
    element.scrollHeight - element.scrollTop - element.clientHeight
  ))).toBeLessThanOrEqual(2);
});

test("an interrupted partial stays truthful even when the initial diff refresh fails", async ({ page }) => {
  await installSocketHarness(page);
  const run = activeRun();
  run.state = "interrupted";
  const events = [
    {
      run_id: run.id,
      seq: 1,
      type: "turn.started",
      payload: { index: 1, request: run.task },
      created_at: createdAt,
    },
    {
      run_id: run.id,
      seq: 2,
      type: "assistant.output.started",
      payload: {
        stream_id: "crash-partial",
        turn_index: 1,
        surface: "conversation",
      },
      created_at: createdAt,
    },
    {
      run_id: run.id,
      seq: 3,
      type: "assistant.output.delta",
      payload: {
        stream_id: "crash-partial",
        turn_index: 1,
        segment_index: 1,
        delta: "Durable partial before the process stopped",
      },
      created_at: createdAt,
    },
  ];
  await mockApp(page, run, events, { failDiff: true });

  await page.goto("/");
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeSocketCount?: () => number;
    }).__traceforgeSocketCount?.() ?? 0
  ))).toBe(1);
  const bubble = page.locator(".assistant-turn.streamed-output");
  await expect(bubble).toContainText("Durable partial before the process stopped");
  await expect(bubble).toContainText("已中断");
  await expect(bubble).toHaveAttribute("aria-busy", "false");
});
