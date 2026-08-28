import { expect, test } from "@playwright/test";
import { expectNoWcagViolations } from "./a11y";

const createdAt = "2026-08-28T00:00:00Z";

function json(body: unknown) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function status(workspace: string) {
  return {
    version: "test",
    workspace,
    last_workspace: workspace,
    model: "deepseek-reasoner",
    base_url: "https://api.deepseek.com/v1",
    api_key_configured: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64_000, context_source: "catalog", steps: 30, repair_cycles: 2 },
  };
}

function provider(efforts = ["auto", "none", "low", "high", "max"]) {
  return {
    model: "deepseek-reasoner",
    base_url: "https://api.deepseek.com/v1",
    credential_source: "environment",
    credential_file: null,
    credential_env: "DEEPSEEK_API_KEY",
    api_key_configured: true,
    context_window: null,
    resolved_context_window: 64_000,
    context_window_source: "catalog",
    supported_reasoning_efforts: efforts,
    default_reasoning_effort: "high",
    reasoning_effort_source: "deepseek_catalog",
    reasoning_effort_catalog_version: "test",
    updated_at: createdAt,
  };
}

function answeredRun(id: string, task: string, summary: string, effort = "auto") {
  return {
    id,
    task,
    workspace: `/tmp/${id}`,
    project_id: null,
    state: "answered",
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: effort,
    turns: [{
      index: 1,
      request: task,
      mode: "agent",
      approval_mode: "automatic",
      reasoning_effort: effort,
      outcome: "answered",
      summary,
      changed_files: [],
      started_at: createdAt,
      completed_at: createdAt,
    }],
    verifier_enabled: true,
    plan: null,
    clarification: null,
    pending_approval: null,
    verification: null,
    plan_gate: null,
    step_count: 0,
    repair_cycles: 0,
    context_tokens: 400,
    context_limit: 64_000,
    error: null,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

test("legacy direct answers render once and hide model transport noise", async ({ page }) => {
  const answer = "SELECT department, COUNT(*) FROM employees GROUP BY department;";
  const run = answeredRun("answered-run", "Write the SQL query", answer);
  const events = [
    { run_id: run.id, seq: 1, type: "turn.started", payload: { index: 1, request: run.task, approval_mode: "automatic", reasoning_effort: "auto" }, created_at: createdAt },
    { run_id: run.id, seq: 2, type: "model.requested", payload: { model: "deepseek-reasoner", requested_effort: "auto", omitted: true }, created_at: createdAt },
    { run_id: run.id, seq: 3, type: "message", payload: { phase: "planning", content: answer }, created_at: createdAt },
    { run_id: run.id, seq: 4, type: "model.requested", payload: { model: "deepseek-reasoner", requested_effort: "auto", omitted: true }, created_at: createdAt },
    { run_id: run.id, seq: 5, type: "turn.completed", payload: { index: 1, outcome: "answered", summary: answer, changed_files: [] }, created_at: createdAt },
    { run_id: run.id, seq: 6, type: "run.completed", payload: { state: "answered" }, created_at: createdAt },
  ];

  await page.route("**/api/status", (route) => route.fulfill(json(status(run.workspace))));
  await page.route("**/api/runs", (route) => route.fulfill(json([run])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json(provider())));
  await page.route("**/api/runs/answered-run", (route) => route.fulfill(json(run)));
  await page.route("**/api/runs/answered-run/events?*", (route) => route.fulfill(json(events)));
  await page.route("**/api/runs/answered-run/diff", (route) => route.fulfill(json({ diff: "" })));

  await page.goto("/");

  await expect(page.locator(".assistant-turn").filter({ hasText: answer })).toHaveCount(1);
  await expect(page.locator(".trace-details")).toHaveCount(0);
  await page.getByRole("button", { name: "任务详情" }).click();
  const timeline = page.locator(".timeline");
  await expect(timeline.getByText("模型调用", { exact: true })).toHaveCount(2);
  await expect(timeline).not.toContainText("model.requested");
  await expect(timeline).not.toContainText("deepseek-reasoner");
  await expect(timeline).not.toContainText("未发送强度字段");
  await expectNoWcagViolations(page, "deduplicated direct answer");
});

test("reasoning picker follows the exact sparse model capability list", async ({ page }) => {
  const workspace = "/tmp/traceforge-picker";
  let submitted: Record<string, unknown> | null = null;
  const created = answeredRun("created-run", "Explain this repository", "Done", "high");

  await page.route("**/api/status", (route) => route.fulfill(json(status(workspace))));
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST") {
      submitted = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill(json(created));
      return;
    }
    await route.fulfill(json([]));
  });
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json(provider())));
  await page.route("**/api/runs/created-run", (route) => route.fulfill(json(created)));
  await page.route("**/api/runs/created-run/events?*", (route) => route.fulfill(json([])));
  await page.route("**/api/runs/created-run/diff", (route) => route.fulfill(json({ diff: "" })));

  await page.setViewportSize({ width: 390, height: 780 });
  await page.goto("/");

  const picker = page.getByLabel("本轮思考强度");
  await expect(picker).toBeVisible();
  await expect(picker.locator("option")).toHaveText([
    "模型默认 · 高", "关闭", "低", "高", "最大",
  ]);
  await expect(picker.locator('option[value="minimal"]')).toHaveCount(0);
  await expect(picker.locator('option[value="medium"]')).toHaveCount(0);
  await expect(picker.locator('option[value="xhigh"]')).toHaveCount(0);
  await picker.selectOption("high");
  await expect(picker).toHaveValue("high");
  await picker.focus();
  await expect(picker).toBeFocused();
  await picker.selectOption("max");
  await expect(picker).toHaveValue("max");
  await picker.selectOption("high");
  await expect(picker).toHaveValue("high");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);
  await expectNoWcagViolations(page, "sparse reasoning picker");

  await page.locator("textarea").fill("Explain this repository");
  await page.getByRole("button", { name: "发送" }).click();
  await expect.poll(() => submitted?.reasoning_effort).toBe("high");
});

test("reasoning capability loading and a non-default singleton stay truthful", async ({ page }) => {
  const workspace = "/tmp/traceforge-fixed-effort";
  let releaseProvider!: () => void;
  const providerReady = new Promise<void>((resolve) => {
    releaseProvider = resolve;
  });

  await page.route("**/api/status", (route) => route.fulfill(json(status(workspace))));
  await page.route("**/api/runs", (route) => route.fulfill(json([])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", async (route) => {
    await providerReady;
    await route.fulfill(json({
      ...provider(["none"]),
      default_reasoning_effort: "none",
    }));
  });

  await page.goto("/");
  await expect(page.getByRole("status", { name: "本轮思考强度：正在读取模型能力" }))
    .toContainText("能力加载中");
  await expect(page.getByText("唯一可用", { exact: true })).toHaveCount(0);

  releaseProvider();
  const fixed = page.getByRole("group", { name: "本轮思考强度：关闭，唯一可用" });
  await expect(fixed).toContainText("关闭");
  await expect(fixed).toContainText("唯一可用");
  await expect(page.locator('select[aria-label="本轮思考强度"]')).toHaveCount(0);
  await expectNoWcagViolations(page, "fixed reasoning capability");
});
