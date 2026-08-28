import { expect, test, type Page } from "@playwright/test";

const createdAt = "2026-08-28T00:00:00Z";

function json(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((ready) => {
    resolve = ready;
  });
  return { promise, resolve };
}

function appStatus() {
  return {
    version: "test",
    workspace: "/tmp/traceforge-continuity",
    last_workspace: "/tmp/traceforge-continuity",
    model: "deepseek-reasoner",
    base_url: "https://api.deepseek.com/v1",
    api_key_configured: true,
    connection_verified: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64_000, context_source: "catalog", steps: 30, repair_cycles: 2 },
  };
}

function provider() {
  return {
    model: "deepseek-reasoner",
    base_url: "https://api.deepseek.com/v1",
    credential_source: "environment",
    credential_file: null,
    credential_env: "DEEPSEEK_API_KEY",
    environment_credential_configured: true,
    api_key_configured: true,
    connection_verified: true,
    verified_at: createdAt,
    context_window: null,
    resolved_context_window: 64_000,
    context_window_source: "catalog",
    supported_reasoning_efforts: ["auto", "low", "high"],
    default_reasoning_effort: "auto",
    reasoning_effort_source: "deepseek_catalog",
    reasoning_effort_catalog_version: "test",
    updated_at: createdAt,
  };
}

function run(
  id: string,
  task: string,
  state: string,
  updatedAt: string,
  projectId: string | null = null,
) {
  const terminal = ["answered", "succeeded", "failed", "cancelled", "rolled_back"]
    .includes(state);
  return {
    id,
    task,
    workspace: `/tmp/${id}`,
    project_id: projectId,
    state,
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: "auto",
    proof_turn_indexes: [],
    turns: [{
      index: 1,
      request: task,
      mode: "agent",
      approval_mode: "automatic",
      reasoning_effort: "auto",
      outcome: terminal ? state : null,
      summary: terminal ? `${task} summary` : null,
      changed_files: [],
      started_at: createdAt,
      completed_at: terminal ? updatedAt : null,
    }],
    verifier_enabled: true,
    plan: null,
    clarification: null,
    pending_approval: null,
    decision_request_id: null,
    decision_kind: null,
    verification: null,
    plan_gate: null,
    step_count: 0,
    repair_cycles: 0,
    context_tokens: 400,
    context_limit: 64_000,
    error: null,
    parent_run_id: null,
    successor_run_id: null,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

async function mockShell(
  page: Page,
  listRuns: () => ReturnType<typeof run>[],
  listProjects: () => Array<Record<string, unknown>> = () => [],
  onListRequest: () => void = () => undefined,
) {
  await page.route(/\/api\/status$/, (route) => route.fulfill(json(appStatus())));
  await page.route(/\/api\/provider$/, (route) => route.fulfill(json(provider())));
  await page.route(/\/api\/projects$/, (route) => route.fulfill(json(listProjects())));
  await page.route(/\/api\/runs$/, (route) => {
    onListRequest();
    return route.fulfill(json(listRuns()));
  });
  await page.route(/\/api\/runs\/[^/?]+\/events(?:\?.*)?$/, (route) => (
    route.fulfill(json([]))
  ));
  await page.route(/\/api\/runs\/[^/?]+\/diff$/, (route) => (
    route.fulfill(json({ diff: "" }))
  ));
  await page.route(/\/api\/runs\/[^/?]+$/, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1);
    const selected = listRuns().find((candidate) => candidate.id === id);
    return route.fulfill(selected ? json(selected) : json({ detail: "not found" }, 404));
  });
}

function runItem(page: Page, task: string) {
  return page.locator(".run-item").filter({ hasText: task });
}

test("session drafts stay isolated by task target and never enter browser storage", async ({
  page,
}) => {
  const project = {
    id: "project-one",
    name: "payments",
    root: "/tmp/payments",
    created_at: createdAt,
    updated_at: createdAt,
    last_opened_at: createdAt,
  };
  const runA = run("run-a", "Task Alpha", "answered", "2026-08-28T00:02:00Z");
  const runB = run("run-b", "Task Beta", "answered", "2026-08-28T00:01:00Z");
  await mockShell(page, () => [runA, runB], () => [project]);
  const canaries = {
    followA: "private-follow-alpha-7b193",
    followB: "private-follow-beta-31ad2",
    direct: "private-direct-draft-988e1",
    project: "private-project-draft-c034f",
  };
  const observedRequests: string[] = [];
  page.on("request", (request) => {
    observedRequests.push(`${request.url()}\n${request.postData() ?? ""}`);
  });

  await page.goto("/");
  const followUp = page.locator(".follow-up-composer");
  await followUp.getByLabel("继续此任务").fill(canaries.followA);
  await followUp.locator('input[type="checkbox"]').check({ force: true });
  await followUp.getByLabel("本轮权限模式").selectOption("manual");
  await followUp.getByLabel("本轮思考强度").selectOption("high");

  await runItem(page, "Task Beta").click();
  await expect(followUp.getByLabel("本轮权限模式")).toHaveValue("automatic");
  await followUp.getByLabel("继续此任务").fill(canaries.followB);
  await followUp.getByLabel("本轮思考强度").selectOption("low");

  await runItem(page, "Task Alpha").click();
  await expect(followUp.getByLabel("继续此任务")).toHaveValue(canaries.followA);
  await expect(followUp.locator('input[type="checkbox"]')).toBeChecked();
  await expect(followUp.getByLabel("本轮权限模式")).toHaveValue("manual");
  await expect(followUp.getByLabel("本轮思考强度")).toHaveValue("high");

  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  const taskComposer = page.locator(".task-composer");
  await taskComposer.locator("textarea").fill(canaries.direct);
  await taskComposer.locator('input[type="checkbox"]').check({ force: true });
  await taskComposer.getByRole("radio", { name: /手动审批/ }).check({ force: true });
  await taskComposer.getByLabel("本轮思考强度").selectOption("high");

  await runItem(page, "Task Beta").click();
  await page.getByRole("button", { name: "在 payments 中新建任务" }).click();
  await expect(taskComposer.getByRole("radio", { name: /自动审批/ })).toBeChecked();
  await taskComposer.locator("textarea").fill(canaries.project);
  await taskComposer.getByLabel("本轮思考强度").selectOption("low");

  await runItem(page, "Task Alpha").click();
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await expect(taskComposer.locator("textarea")).toHaveValue(canaries.direct);
  await expect(taskComposer.locator('input[type="checkbox"]')).toBeChecked();
  await expect(taskComposer.getByRole("radio", { name: /手动审批/ })).toBeChecked();
  await expect(taskComposer.getByLabel("本轮思考强度")).toHaveValue("high");

  await page.getByRole("button", { name: "在 payments 中新建任务" }).click();
  await expect(taskComposer.locator("textarea")).toHaveValue(canaries.project);
  await expect(taskComposer.getByLabel("本轮思考强度")).toHaveValue("low");

  const browserStorage = await page.evaluate(() => ({
    local: { ...window.localStorage },
    session: { ...window.sessionStorage },
    href: window.location.href,
  }));
  expect(browserStorage.local).not.toHaveProperty("traceforge:approval-mode:v1");
  for (const canary of Object.values(canaries)) {
    expect(JSON.stringify(browserStorage)).not.toContain(canary);
    expect(observedRequests.join("\n")).not.toContain(canary);
  }

  await page.setViewportSize({ width: 390, height: 780 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);
  await page.setViewportSize({ width: 1280, height: 720 });

  await page.reload();
  await expect(page.getByRole("heading", { name: "Task Alpha" })).toBeVisible();
  await expect(page.getByLabel("继续此任务")).toHaveValue("");
  await expect(page.getByLabel("本轮权限模式")).toHaveValue("automatic");
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await expect(taskComposer.locator("textarea")).toHaveValue("");
  await expect(taskComposer.getByRole("radio", { name: /自动审批/ })).toBeChecked();
});

test("a background run reaches terminal state in the sidebar without detail cross-talk", async ({
  page,
}) => {
  const active = run("run-active", "Background Build", "executing", "2026-08-28T00:03:00Z");
  const completed = run("run-active", "Background Build", "succeeded", "2026-08-28T00:05:00Z");
  const foreground = run("run-foreground", "Foreground Review", "answered", "2026-08-28T00:02:00Z");
  let foregroundSelected = false;
  let listCalls = 0;
  let backgroundCompleted = false;
  await mockShell(page, () => {
    if (foregroundSelected && listCalls > 1) backgroundCompleted = true;
    return [backgroundCompleted ? completed : active, foreground];
  }, () => [], () => {
    listCalls += 1;
  });
  const backgroundDetailRequests: string[] = [];
  const backgroundSockets: string[] = [];
  page.on("request", (request) => {
    if (/\/api\/runs\/run-active\/(events|diff)/u.test(request.url())) {
      backgroundDetailRequests.push(request.url());
    }
  });
  page.on("websocket", (socket) => {
    if (socket.url().includes("/api/runs/run-active/events")) {
      backgroundSockets.push(socket.url());
    }
  });

  await page.goto("/");
  await expect(runItem(page, "Background Build")).toContainText("正在执行");
  await runItem(page, "Foreground Review").click();
  foregroundSelected = true;
  const detailCountAfterSwitch = backgroundDetailRequests.length;
  const socketCountAfterSwitch = backgroundSockets.length;

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const callsWhileHidden = listCalls;
  await page.waitForTimeout(2_300);
  expect(listCalls).toBe(callsWhileHidden);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
  });

  await expect(runItem(page, "Background Build")).toContainText("已证实", {
    timeout: 7_000,
  });
  expect(listCalls).toBe(callsWhileHidden + 1);
  await expect(page.getByRole("heading", { name: "Foreground Review" })).toBeVisible();
  expect(backgroundDetailRequests).toHaveLength(detailCountAfterSwitch);
  expect(backgroundSockets).toHaveLength(socketCountAfterSwitch);
  const callsAtTerminal = listCalls;
  await page.waitForTimeout(2_300);
  expect(listCalls).toBe(callsAtTerminal);
});

test("late create and follow-up responses cannot steal navigation or enable duplicates", async ({
  page,
}) => {
  const runA = run("run-a", "Stable Alpha", "answered", "2026-08-28T00:02:00Z");
  const runB = run("run-b", "Stable Beta", "answered", "2026-08-28T00:01:00Z");
  const created = run("created-run", "Create in background", "planning", "2026-08-28T00:04:00Z");
  const continued = run("run-a", "Stable Alpha", "planning", "2026-08-28T00:05:00Z");
  const project = {
    id: "background-project",
    name: "background-project",
    root: "/tmp/background-project",
    created_at: createdAt,
    updated_at: createdAt,
    last_opened_at: createdAt,
  };
  let runs = [runA, runB];
  await mockShell(page, () => runs, () => [project]);
  let statusCalls = 0;
  await page.route(/\/api\/status$/, (route) => {
    statusCalls += 1;
    return route.fulfill(statusCalls === 1
      ? json(appStatus())
      : json({ detail: "late status sync failure" }, 500));
  });

  const createSuccess = deferred();
  const createFailure = deferred();
  let createCalls = 0;
  await page.route(/\/api\/runs$/, async (route) => {
    if (route.request().method() !== "POST") {
      await route.fulfill(json(runs));
      return;
    }
    createCalls += 1;
    if (createCalls === 1) {
      await createSuccess.promise;
      runs = [created, runA, runB];
      await route.fulfill(json(created));
      return;
    }
    await createFailure.promise;
    await route.fulfill(json({ detail: "late create failure" }, 500));
  });

  const followUpGate = deferred();
  let followUpCalls = 0;
  await page.route(/\/api\/runs\/run-a\/turns$/, async (route) => {
    followUpCalls += 1;
    await followUpGate.promise;
    runs = [continued, created, runB];
    await route.fulfill(json(continued));
  });
  const followUpFailureGate = deferred();
  let failedFollowUpCalls = 0;
  await page.route(/\/api\/runs\/run-b\/turns$/, async (route) => {
    failedFollowUpCalls += 1;
    await followUpFailureGate.promise;
    await route.fulfill(json({ detail: "late follow-up failure" }, 500));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  const taskComposer = page.locator(".task-composer");
  await taskComposer.locator("textarea").fill("Create in background");
  await taskComposer.getByRole("radio", { name: /手动审批/ }).check({ force: true });
  const send = taskComposer.getByRole("button", { name: "发送" });
  await send.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => createCalls).toBe(1);
  await runItem(page, "Stable Beta").click();
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await expect(taskComposer.locator("textarea")).toBeDisabled();
  await expect(taskComposer.locator("textarea")).toHaveValue("Create in background");
  await expect(send).toBeDisabled();
  await page.getByRole("button", { name: "在 background-project 中新建任务" }).click();
  await expect(page.getByRole("heading", {
    name: "background-project",
  })).toBeVisible();
  await expect(taskComposer.getByRole("radio", { name: /自动审批/ })).toBeChecked();
  createSuccess.resolve();

  await expect(runItem(page, "Create in background")).toBeVisible();
  await expect(page.getByRole("heading", {
    name: "background-project",
  })).toBeVisible();
  await expect(taskComposer.getByRole("radio", { name: /自动审批/ })).toBeChecked();
  await runItem(page, "Stable Beta").click();
  await expect(page.getByRole("heading", { name: "Stable Beta" })).toBeVisible();
  await expect.poll(() => statusCalls).toBeGreaterThan(1);
  await expect(page.locator(".global-error")).toHaveCount(0);
  expect(createCalls).toBe(1);

  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await expect(taskComposer.locator("textarea")).toHaveValue("");
  await expect(taskComposer.getByRole("radio", { name: /手动审批/ })).toBeChecked();
  expect(await page.evaluate(() => localStorage.getItem("traceforge:approval-mode:v1")))
    .toBe("manual");
  await taskComposer.locator("textarea").fill("Retain failed background creation");
  await taskComposer.getByRole("radio", { name: /完全访问/ }).check({ force: true });
  await send.click();
  await expect.poll(() => createCalls).toBe(2);
  await runItem(page, "Stable Beta").click();
  createFailure.resolve();
  await expect(page.getByRole("heading", { name: "Stable Beta" })).toBeVisible();
  await expect(page.locator(".global-error")).toHaveCount(0);
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await expect(taskComposer.locator("textarea")).toHaveValue(
    "Retain failed background creation",
  );
  await expect(taskComposer.getByRole("radio", { name: /完全访问/ })).toBeChecked();
  expect(await page.evaluate(() => localStorage.getItem("traceforge:approval-mode:v1")))
    .toBe("manual");

  await runItem(page, "Stable Alpha").click();
  const followUp = page.locator(".follow-up-composer");
  await followUp.getByLabel("继续此任务").fill("Continue Alpha once");
  const continueButton = followUp.getByRole("button", { name: "继续任务" });
  await continueButton.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => followUpCalls).toBe(1);
  await runItem(page, "Stable Beta").click();
  await runItem(page, "Stable Alpha").click();
  await expect(followUp.getByLabel("继续此任务")).toHaveValue("Continue Alpha once");
  await expect(followUp.getByLabel("继续此任务")).toBeDisabled();
  await expect(continueButton).toBeDisabled();
  followUpGate.resolve();

  await expect(page.locator(".run-header .state-badge")).toHaveText("正在规划");
  await expect(page.getByLabel("继续此任务")).toHaveCount(0);
  expect(followUpCalls).toBe(1);

  await runItem(page, "Stable Beta").click();
  await followUp.getByLabel("继续此任务").fill("Retain failed Beta follow-up");
  await followUp.getByRole("button", { name: "继续任务" }).click();
  await expect.poll(() => failedFollowUpCalls).toBe(1);
  await runItem(page, "Stable Alpha").click();
  followUpFailureGate.resolve();
  await expect(page.getByRole("heading", { name: "Stable Alpha" })).toBeVisible();
  await expect(page.locator(".global-error")).toHaveCount(0);
  await runItem(page, "Stable Beta").click();
  await expect(followUp.getByLabel("继续此任务")).toHaveValue(
    "Retain failed Beta follow-up",
  );
  await expect(followUp.getByLabel("继续此任务")).toBeEnabled();
});
