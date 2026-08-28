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
    connection_verified: true,
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
    environment_credential_configured: true,
    api_key_configured: true,
    connection_verified: true,
    verified_at: createdAt,
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
    proof_turn_indexes: [],
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

test("workspace rules stay visible as provenance without leaking their content", async ({ page }) => {
  const privateCanary = "PRIVATE-WORKSPACE-RULE-CANARY";
  const initialRun = answeredRun("rules-run", "Review the repository", "Reviewed");
  const firstTurn = initialRun.turns[0];
  const run = {
    ...initialRun,
    turns: [
      firstTurn,
      {
        ...firstTurn,
        index: 2,
        request: "Review it again",
        summary: "Reviewed again",
      },
    ],
  };
  const events = [
    { run_id: run.id, seq: 1, type: "turn.started", payload: { index: 1, request: run.task, approval_mode: "automatic", reasoning_effort: "auto" }, created_at: createdAt },
    {
      run_id: run.id,
      seq: 2,
      type: "workspace.instructions.resolved",
      payload: {
        schema_version: "traceforge.workspace-instructions.v1",
        captured_at: createdAt,
        sources: [{
          path: "AGENTS.md",
          scope: ".",
          content_sha256: "a".repeat(64),
          byte_count: 128,
          content: privateCanary,
        }],
        total_bytes: 128,
        snapshot_sha256: "b".repeat(64),
        turn_index: 1,
        status: "loaded",
        authority: "guidance",
        content_private: true,
      },
      created_at: createdAt,
    },
    { run_id: run.id, seq: 3, type: "turn.completed", payload: { index: 1, outcome: "answered", summary: "Reviewed", changed_files: [] }, created_at: createdAt },
    { run_id: run.id, seq: 4, type: "turn.started", payload: { index: 2, request: "Review it again", approval_mode: "automatic", reasoning_effort: "auto" }, created_at: createdAt },
    {
      run_id: run.id,
      seq: 5,
      type: "workspace.instructions.resolved",
      payload: {
        schema_version: "traceforge.workspace-instructions.v1",
        captured_at: createdAt,
        sources: [{
          path: "AGENTS.md",
          scope: ".",
          content_sha256: "c".repeat(64),
          byte_count: 96,
          content: `${privateCanary}-SECOND`,
        }],
        total_bytes: 96,
        snapshot_sha256: "d".repeat(64),
        turn_index: 2,
        status: "loaded",
        authority: "guidance",
        content_private: true,
      },
      created_at: createdAt,
    },
    { run_id: run.id, seq: 6, type: "turn.completed", payload: { index: 2, outcome: "answered", summary: "Reviewed again", changed_files: [] }, created_at: createdAt },
    {
      run_id: run.id,
      seq: 7,
      type: "workspace.instructions.resolved",
      payload: { turn_index: 2, status: "loaded" },
      created_at: createdAt,
    },
  ];

  await page.route("**/api/status", (route) => route.fulfill(json(status(run.workspace))));
  await page.route("**/api/runs", (route) => route.fulfill(json([run])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json(provider())));
  await page.route("**/api/runs/rules-run", (route) => route.fulfill(json(run)));
  await page.route("**/api/runs/rules-run/events?*", (route) => route.fulfill(json(events)));
  await page.route("**/api/runs/rules-run/diff", (route) => route.fulfill(json({ diff: "" })));

  await page.setViewportSize({ width: 390, height: 768 });
  await page.goto("/");

  await expect(page.getByLabel("已载入 1 份工作区规则；仅作为模型上下文，不改变权限或沙箱"))
    .toBeVisible();
  await page.locator(".trace-details > summary").click();
  await expect(page.locator(".trace-details > summary")).toContainText("2 条 Trace");
  await expect(page.locator(".workspace-rules-activity")).toHaveCount(2);
  await expect(page.locator(".workspace-rules-activity").nth(0)).toContainText("第 1 轮");
  await expect(page.locator(".workspace-rules-activity").nth(1)).toContainText("第 2 轮");
  await expect(page.locator(".workspace-rules-activity").nth(1)).toContainText("AGENTS.md · 已载入");
  await expect(page.locator(".workspace-rules-activity").nth(1))
    .toContainText("仅作为项目指引，不改变权限或沙箱");
  await expect(page.locator("body")).not.toContainText(privateCanary);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);

  await page.getByRole("button", { name: "任务详情" }).click();
  await expect(page.locator(".timeline")).toContainText("工作区规则");
  await expect(page.locator(".timeline")).toContainText("第 1 轮 · AGENTS.md · 已载入 · 128 B");
  await expect(page.locator(".timeline")).toContainText("第 2 轮 · AGENTS.md · 已载入 · 96 B");
  await expect(page.locator(".timeline")).toContainText("工作区规则清单格式无效");
  await expectNoWcagViolations(page, "workspace rule provenance");
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

test("a fresh failed draft probe stays unsaved and cannot enable task submission", async ({ page }) => {
  const workspace = "/tmp/traceforge-unverified";
  const savedProvider = {
    ...provider(["auto"]),
    credential_source: "missing",
    credential_file: null,
    credential_env: "OPENAI_API_KEY",
    environment_credential_configured: false,
    api_key_configured: false,
    connection_verified: false,
    verified_at: null,
  };
  let testedDraft: Record<string, unknown> | null = null;
  let saveCalls = 0;

  await page.route("**/api/status", (route) => route.fulfill(json({
    ...status(workspace),
    api_key_configured: false,
    connection_verified: false,
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider/test", async (route) => {
    testedDraft = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill(json({
      ok: false,
      model: "draft-model",
      latency_ms: 12,
      detail: "Model connection failed: unavailable",
      provider: savedProvider,
    }));
  });
  await page.route("**/api/provider", (route) => {
    if (route.request().method() === "PUT") saveCalls += 1;
    return route.fulfill(json(savedProvider));
  });

  await page.goto("/");
  await page.getByRole("button", { name: /需要配置模型/ }).click();
  await page.getByLabel("模型", { exact: true }).fill("draft-model");
  await page.getByLabel("OpenAI 兼容接口地址").fill("https://draft.example/v1");
  await page.locator('input[type="password"]').fill("test-browser-rejected-secret");
  await page.getByRole("button", { name: "测试并保存" }).click();

  await expect(page.getByText("连接检查失败", { exact: true })).toBeVisible();
  await expect(page.getByText("需要凭证", { exact: true })).toBeVisible();
  expect(testedDraft).toMatchObject({
    model: "draft-model",
    base_url: "https://draft.example/v1",
    api_key: "test-browser-rejected-secret",
  });
  expect(saveCalls).toBe(0);

  await page.getByRole("button", { name: "关闭模型设置" }).click();
  await page.locator("textarea").fill("This must remain gated");
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
  await expect(page.locator(".connection")).toHaveAttribute(
    "title",
    "本地服务已就绪，仍需配置模型",
  );
});

test("first-use provider presets pair routes and models before connection testing", async ({ page }) => {
  const workspace = "/tmp/traceforge-provider-onboarding";
  const freshProvider = {
    ...provider(["auto", "none", "low", "medium", "high", "xhigh", "max"]),
    model: "gpt-5.6-sol",
    base_url: null,
    credential_source: "missing",
    credential_file: null,
    credential_env: "OPENAI_API_KEY",
    environment_credential_configured: false,
    api_key_configured: false,
    connection_verified: false,
    verified_at: null,
    context_window_source: "fallback",
    default_reasoning_effort: "medium",
    reasoning_effort_source: "openai_catalog",
  };

  await page.route("**/api/status", (route) => route.fulfill(json({
    ...status(workspace),
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    api_key_configured: false,
    connection_verified: false,
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json(freshProvider)));

  await page.goto("/");
  await page.getByRole("button", { name: /需要配置模型/ }).click();

  const preset = page.getByLabel("服务预设");
  const model = page.getByLabel("模型", { exact: true });
  const baseUrl = page.getByLabel("OpenAI 兼容接口地址");
  const testConnection = page.getByRole("button", { name: "测试并保存" });

  await expect(preset).toBeFocused();
  await expect(preset).toHaveValue("openai");
  await expect(model).toHaveValue("gpt-5.6-sol");
  await expect(baseUrl).toHaveValue("");
  await expect(testConnection).toBeDisabled();
  await expect(page.getByText("首次连接请输入 API Key；也可在高级设置中选择凭证文件。"))
    .toBeVisible();
  await expect(page.getByRole("button", { name: "保存设置" })).toBeEnabled();

  await preset.selectOption("deepseek");
  await expect(model).toHaveValue("deepseek-v4-flash-vision-exp");
  await expect(baseUrl).toHaveValue("https://api.deepseek.com");
  expect(await page.locator("#provider-model-suggestions option").evaluateAll(
    (options) => options.map((option) => (option as HTMLOptionElement).value),
  )).toEqual([
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
  ]);

  await preset.selectOption("openai");
  await expect(model).toHaveValue("gpt-5.6-sol");
  await expect(baseUrl).toHaveValue("");

  await preset.selectOption("custom");
  await expect(model).toHaveValue("gpt-5.6-sol");
  await expect(baseUrl).toHaveValue("");
  await expect(testConnection).toBeDisabled();
  await expect(page.getByText("自定义服务需要填写非官方兼容接口地址。"))
    .toBeVisible();
  await baseUrl.fill("https://gateway.example/v1");
  await model.fill("gateway-model");
  await expect(preset).toHaveValue("custom");
  await expect(testConnection).toBeDisabled();

  await page.locator('input[type="password"]').fill("test-browser-provider-preset-key");
  await expect(testConnection).toBeEnabled();
  await preset.selectOption("openai");
  await expect(model).toHaveValue("gpt-5.6-sol");
  await expect(baseUrl).toHaveValue("");
  await preset.selectOption("custom");
  await expect(model).toHaveValue("gateway-model");
  await expect(baseUrl).toHaveValue("https://gateway.example/v1");
  await expect(testConnection).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 768 });
  const dialog = page.getByRole("dialog", { name: "连接设置" });
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(dialogBox!.x).toBeGreaterThanOrEqual(0);
  expect(dialogBox!.x + dialogBox!.width).toBeLessThanOrEqual(390.5);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);
  await testConnection.scrollIntoViewIfNeeded();
  await expect(testConnection).toBeVisible();
  await expectNoWcagViolations(page, "first-use provider presets");
});

test("clearing a file credential can use an already available environment credential", async ({ page }) => {
  const workspace = "/tmp/traceforge-provider-environment-fallback";
  const fileAndEnvironmentProvider = {
    ...provider(["auto"]),
    credential_source: "file",
    credential_file: "/tmp/traceforge-owner-only.key",
    credential_env: "OPENAI_API_KEY",
    environment_credential_configured: true,
    connection_verified: false,
    verified_at: null,
  };

  await page.route("**/api/status", (route) => route.fulfill(json({
    ...status(workspace),
    connection_verified: false,
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json(fileAndEnvironmentProvider)));

  await page.goto("/");
  await page.getByRole("button", { name: /需要验证模型连接/ }).click();
  await page.getByText("高级设置", { exact: true }).click();
  await page.getByLabel("凭证文件路径").fill("");

  await expect(page.getByRole("button", { name: "测试并保存" })).toBeEnabled();
  await expect(page.getByText("首次连接请输入 API Key；也可在高级设置中选择凭证文件。"))
    .toHaveCount(0);
});

test("a saved but unverified credential asks for verification instead of reconfiguration", async ({ page }) => {
  const workspace = "/tmp/traceforge-awaiting-verification";
  const unverifiedProvider = {
    ...provider(["auto"]),
    connection_verified: false,
    verified_at: null,
  };

  await page.route("**/api/status", (route) => route.fulfill(json({
    ...status(workspace),
    connection_verified: false,
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider", (route) => route.fulfill(json(unverifiedProvider)));

  await page.goto("/");

  await expect(page.getByText("需要验证模型连接", { exact: true })).toBeVisible();
  await expect(page.locator(".connection")).toHaveAttribute(
    "title",
    "本地服务已就绪，需要验证模型连接",
  );
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();

  await page.getByText("需要验证模型连接", { exact: true }).click();
  await expect(page.getByText("凭证已保存，等待验证", { exact: true })).toBeVisible();
});

test("a failed draft probe preserves an older verified connection", async ({ page }) => {
  const workspace = "/tmp/traceforge-verified";
  const savedProvider = provider();
  let saveCalls = 0;

  await page.route("**/api/status", (route) => route.fulfill(json(status(workspace))));
  await page.route("**/api/runs", (route) => route.fulfill(json([])));
  await page.route("**/api/projects", (route) => route.fulfill(json([])));
  await page.route("**/api/provider/test", (route) => route.fulfill(json({
    ok: false,
    model: "rejected-draft-model",
    latency_ms: 9,
    detail: "Model connection failed: unavailable",
    provider: savedProvider,
  })));
  await page.route("**/api/provider", (route) => {
    if (route.request().method() === "PUT") saveCalls += 1;
    return route.fulfill(json(savedProvider));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "模型设置" }).click();
  await page.getByLabel("模型", { exact: true }).fill("rejected-draft-model");
  await page.locator('input[type="password"]').fill("rejected-draft-secret");
  await page.getByRole("button", { name: "测试并保存" }).click();

  await expect(page.getByText("草稿检查失败，已保存连接仍有效", { exact: true }))
    .toBeVisible();
  await expect(page.getByText("连接已验证", { exact: true })).toBeVisible();
  await expect(page.getByLabel("模型", { exact: true })).toHaveValue("rejected-draft-model");
  expect(saveCalls).toBe(0);

  await page.getByRole("button", { name: "关闭模型设置" }).click();
  await page.locator("textarea").fill("The saved provider should remain usable");
  await expect(page.getByRole("button", { name: "发送" })).toBeEnabled();
  await expect(page.locator(".connection")).toHaveAttribute(
    "title",
    "本地服务已就绪，模型连接已验证",
  );
});

test("adding a project lands directly in that project's task composer", async ({ page }) => {
  const workspace = "/tmp/traceforge-project-root";
  const project = {
    id: "project-added",
    name: "billing-service",
    root: "/tmp/billing-service",
    created_at: createdAt,
    updated_at: createdAt,
    last_opened_at: createdAt,
  };
  let submittedProject: Record<string, unknown> | null = null;
  let submittedRun: Record<string, unknown> | null = null;
  const createdRun = {
    ...answeredRun("project-run", "Inspect billing retries", "Done"),
    project_id: project.id,
    workspace: project.root,
  };

  await page.route("**/api/status", (route) => route.fulfill(json(status(workspace))));
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST") {
      submittedRun = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill(json(createdRun));
      return;
    }
    await route.fulfill(json([]));
  });
  await page.route("**/api/runs/project-run", (route) => route.fulfill(json(createdRun)));
  await page.route("**/api/runs/project-run/events?*", (route) => route.fulfill(json([])));
  await page.route("**/api/runs/project-run/diff", (route) => route.fulfill(json({ diff: "" })));
  await page.route("**/api/provider", (route) => route.fulfill(json(provider())));
  await page.route("**/api/filesystem/choose-directory", (route) => route.fulfill(json({
    supported: true,
    path: project.root,
  })));
  await page.route("**/api/projects", async (route) => {
    if (route.request().method() === "POST") {
      submittedProject = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill(json(project));
      return;
    }
    await route.fulfill(json([]));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "添加项目" }).click();

  await expect(page.getByRole("heading", {
    name: "你想在 billing-service 中处理什么？",
  })).toBeVisible();
  await expect(page.locator(".composer-target")).toContainText(project.root);
  await expect(page.locator("textarea")).toBeFocused();
  await expect(page.locator(".project-group")).toContainText("billing-service");
  expect(submittedProject).toEqual({
    name: "billing-service",
    root: project.root,
    create_directory: false,
  });

  await page.locator("textarea").fill("Inspect billing retries");
  await page.getByRole("button", { name: "发送" }).click();
  await expect.poll(() => submittedRun?.project_id).toBe(project.id);
});
