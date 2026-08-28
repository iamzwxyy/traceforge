import { expect, test } from "@playwright/test";

const responsiveRun = {
  id: "responsive-run",
  task: "Check the responsive follow-up composer",
  workspace: "/tmp/traceforge-responsive",
  project_id: null,
  state: "succeeded",
  mode: "agent",
  approval_mode: "automatic",
  reasoning_effort: "high",
  turns: [{
    index: 1,
    request: "Check the responsive follow-up composer",
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: "high",
    outcome: "succeeded",
    summary: "Verified",
    changed_files: [],
    started_at: "2026-08-28T00:00:00Z",
    completed_at: "2026-08-28T00:01:00Z",
  }],
  verifier_enabled: true,
  plan: null,
  clarification: null,
  pending_approval: null,
  verification: null,
  plan_gate: null,
  step_count: 1,
  repair_cycles: 0,
  context_tokens: 1200,
  context_limit: 64000,
  error: null,
  created_at: "2026-08-28T00:00:00Z",
  updated_at: "2026-08-28T00:01:00Z",
};

async function mockStandardCompletedRun(page: import("@playwright/test").Page) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  await page.route("**/api/status", (route) => route.fulfill(json({
    version: "test",
    workspace: responsiveRun.workspace,
    last_workspace: responsiveRun.workspace,
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    api_key_configured: true,
    connection_verified: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64000, context_source: "catalog", steps: 24, repair_cycles: 2 },
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([responsiveRun])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json({
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    credential_source: "environment",
    credential_file: null,
    credential_env: "OPENAI_API_KEY",
    api_key_configured: true,
    connection_verified: true,
    verified_at: "2026-08-28T00:00:00Z",
    context_window: null,
    resolved_context_window: 64000,
    context_window_source: "catalog",
    supported_reasoning_efforts: ["auto", "none", "low", "medium", "high", "xhigh", "max"],
    default_reasoning_effort: "medium",
    reasoning_effort_source: "openai_catalog",
    reasoning_effort_catalog_version: "2026-08-28",
    updated_at: "2026-08-28T00:00:00Z",
  })));
  await page.route("**/api/runs/responsive-run", (route) => route.fulfill(json(responsiveRun)));
  await page.route("**/api/runs/responsive-run/events?*", (route) => route.fulfill(json([])));
  await page.route("**/api/runs/responsive-run/diff", (route) => route.fulfill(json({ diff: "" })));
}

async function expectInsideViewport(
  page: import("@playwright/test").Page,
  locator: import("@playwright/test").Locator,
) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width + 0.5);
}

const viewports = [
  { width: 1366, sidebar: true, inspector: false },
  { width: 1024, sidebar: true, inspector: false },
  { width: 980, sidebar: true, inspector: false },
  { width: 680, sidebar: false, inspector: false },
  { width: 390, sidebar: false, inspector: false },
];

test("new-task layout stays operable without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: viewports[0].width, height: 768 });
  await page.goto("/");
  const heading = page.getByRole("heading", { name: "你想让 TraceForge 帮你做什么？" });
  await page.getByRole("button", { name: "新建任务" }).click();
  await expect(heading).toBeVisible();
  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: 768 });
    await expect(heading).toBeVisible();
    await expect(page.getByLabel("本轮思考强度")).toBeVisible();
    if (viewport.sidebar) await expect(page.locator(".sidebar")).toBeVisible();
    else await expect(page.locator(".sidebar")).toBeHidden();
    if (viewport.inspector) await expect(page.locator(".inspector")).toBeVisible();
    else await expect(page.locator(".inspector")).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
      .toBe(true);
  }

  await page.setViewportSize({ width: 390, height: 768 });
  const history = page.getByRole("button", { name: "任务与项目" });
  await history.click();
  const historyDrawer = page.getByRole("dialog", { name: "任务与项目" });
  await expect(historyDrawer).toBeVisible();
  await expect(page.getByRole("button", { name: "关闭任务与项目" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(historyDrawer).toHaveCount(0);
  await expect(history).toBeFocused();

  await page.setViewportSize({ width: 980, height: 768 });
  const details = page.getByRole("button", { name: "任务详情" });
  await details.click();
  const detailsDrawer = page.getByRole("dialog", { name: "任务详情" });
  await expect(detailsDrawer).toBeVisible();
  await expect(page.getByRole("button", { name: "关闭任务详情" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(detailsDrawer).toHaveCount(0);
  await expect(details).toBeFocused();

  await page.setViewportSize({ width: 1366, height: 768 });
  await expect(details).toHaveAttribute("aria-expanded", "false");
  await details.click();
  await expect(page.locator(".inspector")).toBeVisible();
  const leftHandle = page.getByRole("separator", { name: "调整任务侧栏宽度" });
  const rightHandle = page.getByRole("separator", { name: "调整详情侧栏宽度" });
  const leftBefore = Number(await leftHandle.getAttribute("aria-valuenow"));
  const leftBox = await leftHandle.boundingBox();
  expect(leftBox).not.toBeNull();
  await page.mouse.move(leftBox!.x + leftBox!.width / 2, leftBox!.y + 80);
  await page.mouse.down();
  await page.mouse.move(leftBox!.x + leftBox!.width / 2 + 32, leftBox!.y + 80);
  await page.mouse.up();
  await expect.poll(async () => Number(await leftHandle.getAttribute("aria-valuenow")))
    .toBeGreaterThan(leftBefore);

  const rightBefore = Number(await rightHandle.getAttribute("aria-valuenow"));
  await rightHandle.focus();
  await rightHandle.press("ArrowLeft");
  await expect.poll(async () => Number(await rightHandle.getAttribute("aria-valuenow")))
    .toBeGreaterThan(rightBefore);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);

  const persistedLeft = Number(await leftHandle.getAttribute("aria-valuenow"));
  const persistedRight = Number(await rightHandle.getAttribute("aria-valuenow"));
  await expect.poll(() => page.evaluate(() => localStorage.getItem("traceforge:layout:v1:right-collapsed")))
    .toBe("false");
  await page.reload();
  await expect(page.getByRole("separator", { name: "调整任务侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", String(persistedLeft));
  await expect(page.getByRole("separator", { name: "调整详情侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", String(persistedRight));

  await page.evaluate(() => {
    localStorage.setItem("traceforge:layout:v1:left-width", "99999");
    localStorage.setItem("traceforge:layout:v1:right-width", "not-a-number");
  });
  await page.reload();
  await expect(page.getByRole("separator", { name: "调整任务侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", "276");
  await expect(page.getByRole("separator", { name: "调整详情侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", "390");
});

test("terminal follow-up controls stay operable in a narrow main column", async ({ page }) => {
  await mockStandardCompletedRun(page);
  await page.setViewportSize({ width: 768, height: 768 });
  await page.goto("/");

  const prompt = page.getByLabel("继续此任务");
  const approval = page.getByLabel("本轮权限模式");
  const reasoning = page.getByLabel("本轮思考强度");
  const submit = page.getByRole("button", { name: "继续任务" });
  await expect(prompt).toBeVisible();
  await prompt.fill("继续检查");
  for (const control of [approval, reasoning, submit]) {
    await expect(control).toBeVisible();
    await expectInsideViewport(page, control);
  }
  await submit.click({ trial: true });

  await page.setViewportSize({ width: 1024, height: 768 });
  await page.getByRole("button", { name: "任务详情" }).click();
  await expect(page.locator(".inspector")).toBeVisible();
  for (const control of [approval, reasoning, submit]) {
    await expectInsideViewport(page, control);
  }
  await submit.click({ trial: true });
});
