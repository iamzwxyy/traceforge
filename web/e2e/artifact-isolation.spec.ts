import { expect, test } from "@playwright/test";

const createdAt = "2026-08-28T00:00:00Z";

function run(id: string, task: string, minute: number) {
  const updatedAt = `2026-08-28T00:${String(minute).padStart(2, "0")}:00Z`;
  return {
    id,
    task,
    workspace: `/tmp/${id}`,
    project_id: null,
    state: "succeeded",
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: "auto",
    turns: [{
      index: 1,
      request: task,
      mode: "agent",
      approval_mode: "automatic",
      reasoning_effort: "auto",
      outcome: "succeeded",
      summary: `${task} finished`,
      changed_files: [`${id}.txt`],
      started_at: createdAt,
      completed_at: updatedAt,
    }],
    verifier_enabled: true,
    plan: null,
    clarification: null,
    pending_approval: null,
    verification: {
      verdict: "pass",
      summary: `${task} verified`,
      findings: [],
      checked_at: updatedAt,
    },
    plan_gate: null,
    step_count: 1,
    repair_cycles: 0,
    context_tokens: 100,
    context_limit: 64_000,
    error: null,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

const runA = run("run-a", "任务 A", 1);
const runB = run("run-b", "任务 B", 2);

function proofPack(selectedRun: typeof runA) {
  return {
    schema_version: "traceforge.proof-pack.v1",
    generated_at: selectedRun.updated_at,
    run_id: selectedRun.id,
    task: selectedRun.task,
    workspace: selectedRun.workspace,
    project_id: null,
    mode: "agent",
    turns: selectedRun.turns,
    state: "succeeded",
    proof_status: "proven",
    plan: null,
    plan_gate: null,
    changed_files: [`${selectedRun.id}.txt`],
    diff: `DIFF-${selectedRun.id.toUpperCase()}`,
    diff_source: "completion_event",
    diff_sha256: `${selectedRun.id}-diff-sha`,
    checks_fresh: true,
    verification: selectedRun.verification,
    rollback: {
      status: "available",
      conflict_aware: true,
      restored: [],
      removed: [],
      conflicts: [],
    },
    command_sandbox: {
      status: "not_used",
      backends: [],
      sandboxed_commands: 0,
      bypassed_commands: 0,
      policy_only_commands: 0,
      not_executed_commands: 0,
    },
    event_count: 3,
    event_chain_sha256: `${selectedRun.id}-event-sha`,
    step_count: 1,
    repair_cycles: 0,
    created_at: selectedRun.created_at,
    updated_at: selectedRun.updated_at,
    evidence_sha256: `EVIDENCE-${selectedRun.id.toUpperCase()}`,
  };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function afterBrowserCommit(page: import("@playwright/test").Page) {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

function json(body: unknown) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

async function mockTwoRuns(
  page: import("@playwright/test").Page,
  options: {
    waitForDiffA?: Promise<void>;
    onDiffA?: () => void;
    waitForDiffB?: Promise<void>;
    onDiffB?: () => void;
    waitForProofA?: Promise<void>;
    onProofA?: () => void;
    waitForProofB?: Promise<void>;
    onProofB?: () => void;
    waitForOpenA?: Promise<void>;
    onOpenA?: () => void;
    onRollback?: (runId: string) => void;
  } = {},
) {
  await page.route("**/api/status", (route) => route.fulfill(json({
    version: "test",
    workspace: runA.workspace,
    last_workspace: runA.workspace,
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    api_key_configured: true,
    connection_verified: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64_000, context_source: "catalog", steps: 30, repair_cycles: 2 },
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([runA, runB])));
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
  await page.route(/\/api\/runs\/(run-a|run-b)$/, (route) => {
    const selectedRun = route.request().url().endsWith("run-a") ? runA : runB;
    return route.fulfill(json(selectedRun));
  });
  await page.route(/\/api\/runs\/(run-a|run-b)\/events\?/, (route) => (
    route.fulfill(json([]))
  ));
  await page.route(/\/api\/runs\/(run-a|run-b)\/diff$/, async (route) => {
    const isA = route.request().url().includes("/run-a/");
    if (isA) {
      options.onDiffA?.();
      await options.waitForDiffA;
    } else {
      options.onDiffB?.();
      await options.waitForDiffB;
    }
    await route.fulfill(json({ diff: `DIFF-${isA ? "RUN-A" : "RUN-B"}` }));
  });
  await page.route(/\/api\/runs\/(run-a|run-b)\/open-workspace$/, async (route) => {
    const isA = route.request().url().includes("/run-a/");
    if (isA) {
      options.onOpenA?.();
      await options.waitForOpenA;
    }
    await route.fulfill(json(isA
      ? { supported: false, opened: false, application: null }
      : { supported: true, opened: true, application: "Finder" }));
  });
  await page.route(/\/api\/runs\/(run-a|run-b)\/rollback$/, async (route) => {
    const runId = route.request().url().includes("/run-a/") ? "run-a" : "run-b";
    options.onRollback?.(runId);
    await route.fulfill(json({ restored: [], removed: [], conflicts: [] }));
  });
  await page.route(/\/api\/runs\/(run-a|run-b)\/proof-pack$/, async (route) => {
    const isA = route.request().url().includes("/run-a/");
    if (isA) {
      options.onProofA?.();
      await options.waitForProofA;
    } else {
      options.onProofB?.();
      await options.waitForProofB;
    }
    await route.fulfill(json(proofPack(isA ? runA : runB)));
  });
}

test("late diff responses cannot cross task boundaries", async ({ page }) => {
  const diffA = deferred();
  let diffARequested = false;
  await mockTwoRuns(page, {
    waitForDiffA: diffA.promise,
    onDiffA: () => { diffARequested = true; },
  });

  await page.goto("/");
  await expect.poll(() => diffARequested).toBe(true);
  await page.locator(".run-item").filter({ hasText: "任务 B" }).click();
  await expect(page.getByRole("heading", { name: "任务 B" })).toBeVisible();
  await page.getByRole("button", { name: "任务详情" }).click();
  await page.getByRole("button", { name: "差异" }).click();
  await expect(page.locator(".diff-view")).toContainText("DIFF-RUN-B");

  const lateResponse = page.waitForResponse(/\/api\/runs\/run-a\/diff$/);
  diffA.resolve();
  await (await lateResponse).finished();
  await afterBrowserCommit(page);
  await expect(page.locator(".diff-view")).toContainText("DIFF-RUN-B");
  await expect(page.locator(".diff-view")).not.toContainText("DIFF-RUN-A");
});

test("the selected task owns visible controls while its diff is pending", async ({ page }) => {
  const diffB = deferred();
  let diffBRequested = false;
  let rollbackRunId: string | null = null;
  await mockTwoRuns(page, {
    waitForDiffB: diffB.promise,
    onDiffB: () => { diffBRequested = true; },
    onRollback: (runId) => { rollbackRunId = runId; },
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "任务 A" })).toBeVisible();
  await page.locator(".run-item").filter({ hasText: "任务 B" }).click();
  await expect.poll(() => diffBRequested).toBe(true);
  await expect(page.getByRole("heading", { name: "任务 B" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "任务 A" })).not.toBeVisible();

  await page.getByRole("button", { name: "回滚", exact: true }).click();
  await page.getByRole("button", { name: "回滚文件" }).click();
  await expect.poll(() => rollbackRunId).toBe("run-b");

  const pendingResponse = page.waitForResponse(/\/api\/runs\/run-b\/diff$/);
  diffB.resolve();
  await (await pendingResponse).finished();
});

test("a stale workspace-open failure cannot leak into the next task", async ({ page }) => {
  const openA = deferred();
  let openARequested = false;
  await mockTwoRuns(page, {
    waitForOpenA: openA.promise,
    onOpenA: () => { openARequested = true; },
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "任务 A" })).toBeVisible();
  await page.getByRole("button", { name: "打开目录" }).click();
  await expect.poll(() => openARequested).toBe(true);
  await expect(page.getByRole("button", { name: "正在打开" })).toBeDisabled();

  await page.locator(".run-item").filter({ hasText: "任务 B" }).click();
  await expect(page.getByRole("heading", { name: "任务 B" })).toBeVisible();
  await expect(page.getByRole("button", { name: "打开目录" })).toBeEnabled();

  const lateResponse = page.waitForResponse(/\/api\/runs\/run-a\/open-workspace$/);
  openA.resolve();
  await (await lateResponse).finished();
  await afterBrowserCommit(page);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("detached event streams cannot update the newly selected task", async ({ page }) => {
  await page.addInitScript(() => {
    class MockWebSocket {
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      readonly url: string;

      constructor(url: string | URL) {
        this.url = String(url);
        sockets.set(this.url, this);
        queueMicrotask(() => this.onopen?.(new Event("open")));
      }

      close() {
        this.onclose?.(new CloseEvent("close"));
      }

      send() {}
    }

    const sockets = new Map<string, MockWebSocket>();
    const harness = globalThis as typeof globalThis & {
      __traceforgeHasSocket?: (runId: string) => boolean;
      __traceforgeEmitSocket?: (runId: string, data: unknown) => void;
    };
    harness.__traceforgeHasSocket = (runId) => (
      [...sockets.keys()].some((url) => url.includes(`/runs/${runId}/events`))
    );
    harness.__traceforgeEmitSocket = (runId, data) => {
      const socket = [...sockets.entries()]
        .find(([url]) => url.includes(`/runs/${runId}/events`))?.[1];
      socket?.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
    };
    Object.defineProperty(globalThis, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
    });
  });
  await mockTwoRuns(page);

  await page.goto("/");
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeHasSocket?: (runId: string) => boolean;
    }).__traceforgeHasSocket?.("run-a") ?? false
  ))).toBe(true);
  await page.locator(".run-item").filter({ hasText: "任务 B" }).click();
  await expect(page.getByRole("heading", { name: "任务 B" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __traceforgeHasSocket?: (runId: string) => boolean;
    }).__traceforgeHasSocket?.("run-b") ?? false
  ))).toBe(true);
  await page.getByRole("button", { name: "任务详情" }).click();
  await page.getByRole("button", { name: "差异" }).click();

  await page.evaluate(() => {
    const emit = (globalThis as typeof globalThis & {
      __traceforgeEmitSocket?: (runId: string, data: unknown) => void;
    }).__traceforgeEmitSocket;
    emit?.("run-b", {
      run_id: "run-b",
      seq: 1,
      type: "diff.updated",
      payload: { diff: "DIFF-RUN-B-LIVE" },
      created_at: "2026-08-28T00:03:00Z",
    });
  });
  await expect(page.locator(".diff-view")).toContainText("DIFF-RUN-B-LIVE");

  await page.evaluate(() => {
    const emit = (globalThis as typeof globalThis & {
      __traceforgeEmitSocket?: (runId: string, data: unknown) => void;
    }).__traceforgeEmitSocket;
    emit?.("run-b", {
      run_id: "run-a",
      seq: 98,
      type: "diff.updated",
      payload: { diff: "DIFF-WRONG-RUN-ID" },
      created_at: "2026-08-28T00:03:30Z",
    });
  });
  await afterBrowserCommit(page);
  await expect(page.locator(".diff-view")).not.toContainText("DIFF-WRONG-RUN-ID");

  await page.evaluate(() => {
    const emit = (globalThis as typeof globalThis & {
      __traceforgeEmitSocket?: (runId: string, data: unknown) => void;
    }).__traceforgeEmitSocket;
    emit?.("run-a", {
      run_id: "run-a",
      seq: 99,
      type: "diff.updated",
      payload: { diff: "DIFF-RUN-A-LATE" },
      created_at: "2026-08-28T00:04:00Z",
    });
  });
  await afterBrowserCommit(page);
  await expect(page.locator(".diff-view")).toContainText("DIFF-RUN-B-LIVE");
  await expect(page.locator(".diff-view")).not.toContainText("DIFF-RUN-A-LATE");
});

test("proof dialog waits for evidence owned by the selected task", async ({ page }) => {
  const proofA = deferred();
  const proofB = deferred();
  let proofARequested = false;
  let proofBRequested = false;
  await mockTwoRuns(page, {
    onProofA: () => { proofARequested = true; },
    waitForProofA: proofA.promise,
    onProofB: () => { proofBRequested = true; },
    waitForProofB: proofB.promise,
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "任务 A" })).toBeVisible();
  await page.getByRole("button", { name: "查看证据" }).click();
  await expect.poll(() => proofARequested).toBe(true);
  await page.getByRole("button", { name: "关闭证据包" }).click();

  await page.locator(".run-item").filter({ hasText: "任务 B" }).click();
  await expect(page.getByRole("heading", { name: "任务 B" })).toBeVisible();
  await page.getByRole("button", { name: "查看证据" }).click();
  await expect.poll(() => proofBRequested).toBe(true);
  const dialog = page.getByRole("dialog", { name: "证据包" });
  await expect(dialog.getByText("正在汇总持久化证据…")).toBeVisible();

  const lateResponse = page.waitForResponse(/\/api\/runs\/run-a\/proof-pack$/);
  proofA.resolve();
  await (await lateResponse).finished();
  await afterBrowserCommit(page);
  await expect(dialog.getByText("正在汇总持久化证据…")).toBeVisible();
  await expect(dialog).not.toContainText("任务 A");
  await expect(dialog).not.toContainText("EVIDENCE-RUN-A");

  proofB.resolve();
  await expect(dialog.getByText("任务 B", { exact: true })).toBeVisible();
  await expect(dialog).toContainText("EVIDENCE-RUN-B");
  await expect(dialog.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", "/api/runs/run-b/proof-pack.md");
});
