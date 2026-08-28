import { expect, test, type Page } from "@playwright/test";

const createdAt = "2026-08-28T00:00:00Z";

type MockRun = Record<string, unknown>;

function json(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function baseRun(
  id: string,
  task: string,
  state: string,
  overrides: MockRun = {},
): MockRun {
  const completed = ["answered", "succeeded", "failed", "cancelled", "rolled_back"]
    .includes(state);
  return {
    id,
    task,
    workspace: "/tmp/durable-decisions",
    project_id: null,
    parent_run_id: null,
    successor_run_id: null,
    state,
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: "auto",
    proof_turn_indexes: [],
    decision_request_id: null,
    decision_kind: null,
    turns: [{
      index: 1,
      request: task,
      mode: "agent",
      approval_mode: "automatic",
      reasoning_effort: "auto",
      outcome: completed ? "succeeded" : "in_progress",
      summary: completed ? "Completed safely" : "",
      changed_files: completed ? ["note.txt"] : [],
      started_at: createdAt,
      completed_at: completed ? createdAt : null,
    }],
    verifier_enabled: false,
    plan: null,
    clarification: null,
    pending_approval: null,
    verification: null,
    plan_gate: null,
    step_count: 0,
    repair_cycles: 0,
    context_tokens: 120,
    context_limit: 64_000,
    error: null,
    created_at: createdAt,
    updated_at: createdAt,
    ...overrides,
  };
}

async function mockShell(
  page: Page,
  listRuns: () => MockRun[],
  getRun: (id: string) => MockRun,
  shouldFailSynchronization: () => boolean = () => false,
) {
  await page.route("**/api/status", (route) => route.fulfill(json({
    version: "test",
    workspace: "/tmp/durable-decisions",
    last_workspace: "/tmp/durable-decisions",
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    api_key_configured: true,
    connection_verified: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64_000, context_source: "catalog", steps: 30, repair_cycles: 2 },
  })));
  await page.route("**/api/runs", (route) => (
    shouldFailSynchronization()
      ? route.fulfill(json({ detail: "simulated synchronization failure" }, 500))
      : route.fulfill(json(listRuns()))
  ));
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
  await page.route(/\/api\/runs\/([^/?]+)$/, (route) => {
    if (shouldFailSynchronization()) {
      return route.fulfill(json({ detail: "simulated synchronization failure" }, 500));
    }
    const id = new URL(route.request().url()).pathname.split("/").at(-1) ?? "";
    return route.fulfill(json(getRun(id)));
  });
  await page.route(/\/api\/runs\/[^/]+\/events\?/, (route) => route.fulfill(json([])));
  await page.route(/\/api\/runs\/[^/]+\/diff$/, (route) => (
    shouldFailSynchronization()
      ? route.fulfill(json({ detail: "simulated synchronization failure" }, 500))
      : route.fulfill(json({ diff: "" }))
  ));
}

test("a committed decision stays locked when only metadata synchronization fails", async ({
  page,
}) => {
  const current = baseRun("sync-run", "Keep the accepted decision locked", "awaiting_plan_approval", {
    decision_request_id: "sync-plan-request",
    decision_kind: "plan",
    plan: {
      summary: "Commit once",
      steps: [{ id: "one", title: "Commit once", description: "Safely" }],
      acceptance_checks: [{
        id: "check",
        label: "Accepted once",
        command: null,
        status: "pending",
        exit_code: null,
        evidence: "",
      }],
      impacted_files: [],
      risks: [],
      approach: "Keep the accepted decision locked.",
      markdown: "# Implementation plan\n\nCommit once.",
    },
    plan_gate: {
      decision: "approval_required",
      risk: "medium",
      reasons: ["Review required"],
      assessed_at: createdAt,
    },
  });
  let failSynchronization = false;
  await mockShell(
    page,
    () => [current],
    () => current,
    () => failSynchronization,
  );
  let decisionCalls = 0;
  await page.route("**/api/runs/sync-run/plan-decision", async (route) => {
    decisionCalls += 1;
    failSynchronization = true;
    await route.fulfill(json({ accepted: true }, 202));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "批准并执行" }).click();

  await expect.poll(() => decisionCalls).toBe(1);
  await expect(page.getByRole("button", { name: "正在批准" })).toBeDisabled();
  await expect(page.locator(".decision-error")).toHaveCount(0);
  await expect(page.locator(".global-error")).toContainText("操作已成功接收，但状态同步失败");
  await page.getByRole("button", { name: "正在批准" }).evaluate((button: HTMLButtonElement) => {
    button.click();
  });
  expect(decisionCalls).toBe(1);
});

test("clarification, plan, and action decisions are request-bound and single-flight", async ({
  page,
}) => {
  let current = baseRun("decision-run", "Make one durable decision", "awaiting_plan_approval", {
    decision_request_id: "plan-request-1",
    decision_kind: "plan",
    plan: {
      summary: "Review the durable plan",
      steps: [{ id: "one", title: "Do one thing", description: "Safely" }],
      acceptance_checks: [{
        id: "check",
        label: "The result is safe",
        command: null,
        status: "pending",
        exit_code: null,
        evidence: "",
      }],
      impacted_files: ["note.txt"],
      risks: [],
      approach: "Keep the decision durable.",
      markdown: "# Implementation plan\n\nKeep the decision durable.",
    },
    plan_gate: {
      decision: "approval_required",
      risk: "medium",
      reasons: ["The user requested review"],
      assessed_at: createdAt,
    },
  });
  await mockShell(page, () => [current], () => current);

  const planGate = deferred();
  const answerGate = deferred();
  const actionGate = deferred();
  const calls = { plan: 0, answer: 0, action: 0 };
  let planPayload: MockRun = {};
  let answerPayload: MockRun = {};
  let actionPayload: MockRun = {};
  await page.route("**/api/runs/decision-run/plan-decision", async (route) => {
    calls.plan += 1;
    planPayload = route.request().postDataJSON() as MockRun;
    await planGate.promise;
    await route.fulfill(json({ accepted: true }, 202));
  });
  await page.route("**/api/runs/decision-run/answers", async (route) => {
    calls.answer += 1;
    answerPayload = route.request().postDataJSON() as MockRun;
    await answerGate.promise;
    await route.fulfill(json({ accepted: true }, 202));
  });
  await page.route("**/api/runs/decision-run/actions/action-request-1/decision", async (route) => {
    calls.action += 1;
    actionPayload = route.request().postDataJSON() as MockRun;
    await actionGate.promise;
    await route.fulfill(json({ accepted: true }, 202));
  });

  await page.goto("/");
  const approvePlan = page.getByRole("button", { name: "批准并执行" });
  await approvePlan.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => calls.plan).toBe(1);
  await expect(page.locator(".plan-panel")).toHaveAttribute("aria-busy", "true");
  await expect(page.getByRole("button", { name: "正在批准" })).toBeDisabled();
  expect(planPayload).toMatchObject({ request_id: "plan-request-1", decision: "approve" });
  const planResponse = page.waitForResponse(/\/plan-decision$/);
  planGate.resolve();
  await planResponse;
  await expect(page.getByRole("button", { name: "正在批准" })).toBeDisabled();

  current = baseRun("decision-run", "Make one durable decision", "awaiting_clarification", {
    decision_request_id: "clarification-request-1",
    decision_kind: "clarification",
    clarification: {
      round: 1,
      questions: [{
        id: "scope",
        prompt: "Which scope?",
        options: [{
          id: "safe",
          label: "Safe scope",
          description: "Keep the change narrow",
          recommended: true,
        }],
      }],
    },
  });
  await page.reload();
  await page.getByRole("radio", { name: /Safe scope/ }).check();
  const answerButton = page.getByRole("button", { name: "继续", exact: true });
  await answerButton.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => calls.answer).toBe(1);
  await expect(page.locator(".clarification-panel")).toHaveAttribute("aria-busy", "true");
  expect(answerPayload).toMatchObject({
    request_id: "clarification-request-1",
    answers: [{ question_id: "scope", option_id: "safe" }],
  });
  const answerResponse = page.waitForResponse(/\/answers$/);
  answerGate.resolve();
  await answerResponse;
  await expect(page.getByRole("button", { name: "正在提交" })).toBeDisabled();

  current = baseRun("decision-run", "Make one durable decision", "awaiting_action_approval", {
    decision_request_id: "action-request-1",
    decision_kind: "action",
    pending_approval: {
      id: "action-request-1",
      tool_call: { id: "command-1", name: "run_command", arguments: { argv: ["git", "status"] } },
      summary: "Inspect repository status",
      reason: "Manual mode requires approval",
      risk: "elevated",
      approval_mode: "manual",
      policy_decision: "ask",
      sandbox_bypass_on_approve: false,
    },
  });
  await page.reload();
  const approveAction = page.getByRole("button", { name: "批准并继续" });
  await approveAction.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => calls.action).toBe(1);
  await expect(page.locator(".approval-panel")).toHaveAttribute("aria-busy", "true");
  expect(actionPayload).toEqual({ approved: true });
  const actionResponse = page.waitForResponse(/\/actions\/action-request-1\/decision$/);
  actionGate.resolve();
  await actionResponse;
  await expect(page.getByRole("button", { name: "正在批准" })).toBeDisabled();
});

test("rollback stays inspectable while pending, keeps failures retryable, and shows results", async ({
  page,
}) => {
  let current = baseRun("rollback-run", "Safely rollback files", "succeeded", {
    proof_turn_indexes: [1],
    verification: {
      verdict: "pass",
      summary: "Verified",
      findings: [],
      checked_at: createdAt,
    },
  });
  await mockShell(page, () => [current], () => current);
  const firstAttempt = deferred();
  let rollbackCalls = 0;
  await page.route("**/api/runs/rollback-run/rollback", async (route) => {
    rollbackCalls += 1;
    if (rollbackCalls === 1) {
      await firstAttempt.promise;
      await route.fulfill(json({ detail: "simulated rollback failure" }, 500));
      return;
    }
    current = { ...current, state: "rolled_back", updated_at: "2026-08-28T00:01:00Z" };
    await route.fulfill(json({
      restored: ["restored.txt"],
      removed: ["created.txt"],
      conflicts: ["user-edited.txt"],
    }));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "回滚", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "回滚本次运行？" });
  const confirm = dialog.getByRole("button", { name: "回滚文件" });
  await confirm.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => rollbackCalls).toBe(1);
  await expect(dialog).toHaveAttribute("aria-busy", "true");
  await expect(dialog.getByRole("button", { name: "正在回滚" })).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "取消" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();

  const failedResponse = page.waitForResponse(/\/rollback$/);
  firstAttempt.resolve();
  await failedResponse;
  await expect(dialog.getByRole("alert")).toContainText("simulated rollback failure");
  await expect(page.getByRole("alert")).toHaveCount(1);
  await expect(dialog.getByRole("button", { name: "回滚文件" })).toBeEnabled();

  await dialog.getByRole("button", { name: "回滚文件" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText("已回滚，用户后续修改已保留", { exact: true })).toBeVisible();
  await expect(page.getByText("已恢复 · 1", { exact: true })).toBeVisible();
  await expect(page.getByText("已移除 · 1", { exact: true })).toBeVisible();
  await expect(page.getByText("保留冲突 · 1", { exact: true })).toBeVisible();
  await expect(page.locator(".rollback-summary-focus")).toBeFocused();
  expect(rollbackCalls).toBe(2);
});

test("continuing a rolled-back task selects a linked successor exactly once", async ({
  page,
}) => {
  const parent = baseRun("parent-rollback", "Original task", "rolled_back");
  const linkedParent = { ...parent, successor_run_id: "successor-run" };
  const successor = baseRun("successor-run", "Continue from the current workspace", "planning", {
    parent_run_id: "parent-rollback",
  });
  let runs = [parent];
  const byId = (id: string) => runs.find((run) => run.id === id) ?? parent;
  await mockShell(page, () => runs, byId);
  const followUpGate = deferred();
  let followUpCalls = 0;
  let followUpPayload: MockRun = {};
  await page.route("**/api/runs/parent-rollback/turns", async (route) => {
    followUpCalls += 1;
    followUpPayload = route.request().postDataJSON() as MockRun;
    await followUpGate.promise;
    runs = [successor, linkedParent];
    await route.fulfill(json(successor));
  });

  await page.goto("/");
  await expect(page.getByText(/新的安全快照边界/)).toBeVisible();
  const composer = page.locator(".follow-up-composer");
  await composer.getByLabel("继续此任务").fill("Continue from the current workspace");
  const submit = composer.getByRole("button", { name: "继续任务" });
  await submit.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => followUpCalls).toBe(1);
  await expect(composer).toHaveAttribute("aria-busy", "true");
  await expect(composer.getByLabel("继续此任务")).toBeDisabled();
  expect(followUpPayload).toMatchObject({
    prompt: "Continue from the current workspace",
    mode: "agent",
  });

  followUpGate.resolve();
  await expect(page.getByRole("heading", { name: "Continue from the current workspace" }))
    .toBeVisible();
  await expect(page.getByText("续自回滚任务 PARENT-R", { exact: true })).toBeVisible();
  const parentItem = page.locator(".run-item").filter({ hasText: "Original task" });
  await expect(parentItem).toBeVisible();
  await parentItem.click();
  await expect(page.getByRole("button", { name: "打开续跑任务" })).toBeVisible();
  await expect(page.getByLabel("继续此任务")).toHaveCount(0);
  await page.getByRole("button", { name: "打开续跑任务" }).click();
  await expect(page.getByRole("heading", { name: "Continue from the current workspace" }))
    .toBeVisible();
  expect(followUpCalls).toBe(1);
});

test("a conflicting successor refreshes the parent into linked navigation", async ({ page }) => {
  const parent = baseRun("conflict-parent", "Original rollback", "rolled_back");
  const linkedParent = { ...parent, successor_run_id: "other-successor" };
  const successor = baseRun("other-successor", "Created in another client", "planning", {
    parent_run_id: "conflict-parent",
  });
  let runs = [parent];
  await mockShell(
    page,
    () => runs,
    (id) => runs.find((candidate) => candidate.id === id) ?? linkedParent,
  );
  let calls = 0;
  await page.route("**/api/runs/conflict-parent/turns", async (route) => {
    calls += 1;
    runs = [successor, linkedParent];
    await route.fulfill(json({ detail: "This rolled-back run was already continued" }, 409));
  });

  await page.goto("/");
  const composer = page.locator(".follow-up-composer");
  await composer.getByLabel("继续此任务").fill("Create my competing successor");
  await composer.getByRole("button", { name: "继续任务" }).click();

  await expect.poll(() => calls).toBe(1);
  await expect(page.getByRole("button", { name: "打开续跑任务" })).toBeVisible();
  await expect(page.getByLabel("继续此任务")).toHaveCount(0);
  await page.getByRole("button", { name: "打开续跑任务" }).click();
  await expect(page.getByRole("heading", { name: "Created in another client" })).toBeVisible();
});

test("an old open-workspace failure cannot leak through an A-B-A selection cycle", async ({
  page,
}) => {
  const runA = baseRun("workspace-a", "Workspace A", "succeeded");
  const runB = baseRun("workspace-b", "Workspace B", "succeeded");
  const runs = [runA, runB];
  await mockShell(page, () => runs, (id) => (
    runs.find((candidate) => candidate.id === id) ?? runA
  ));
  const openGate = deferred();
  await page.route("**/api/runs/workspace-a/open-workspace", async (route) => {
    await openGate.promise;
    await route.fulfill(json({ detail: "old Finder failure" }, 500));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "打开目录" }).click();
  await page.locator(".run-item").filter({ hasText: "Workspace B" }).click();
  await page.locator(".run-item").filter({ hasText: "Workspace A" }).click();
  openGate.resolve();

  await expect(page.getByRole("heading", { name: "Workspace A" })).toBeVisible();
  await expect(page.locator(".global-error")).toHaveCount(0);
});
