import { expect, test, type Page } from "@playwright/test";

const createdAt = "2026-08-31T06:00:00Z";
const alphaId = "project_8ed3f6ad685b959e";
const betaId = "project_f44e64e75f3948e9";

function json(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function projectScopeRun(
  state: "awaiting_clarification" | "interrupted" | "answered",
  updatedAt: string,
) {
  const waiting = state === "awaiting_clarification";
  const terminal = state === "answered";
  const scope = waiting ? null : {
    path: "alpha",
    label: "alpha",
    markers: ["go.mod", "README.md"],
    selected_by: "clarification",
    identity: "1:2:3",
    root_listed: true,
    evidence_read: ["README.md"],
  };
  const candidates = waiting ? [
    {
      id: alphaId,
      path: "alpha",
      label: "alpha",
      description: "Go 项目 · go.mod, README.md",
      markers: ["go.mod", "README.md"],
      identity: "1:2:3",
    },
    {
      id: betaId,
      path: "beta",
      label: "beta",
      description: "Node.js 项目 · package.json, README.md",
      markers: ["package.json", "README.md"],
      identity: "1:3:4",
    },
  ] : [];
  return {
    id: "project-scope-run",
    task: "介绍一下项目",
    workspace: "/tmp/project-scope-workspace",
    project_id: null,
    parent_run_id: null,
    successor_run_id: null,
    state,
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: "auto",
    proof_turn_indexes: [],
    decision_request_id: waiting ? "project-scope-decision" : null,
    decision_kind: waiting ? "clarification" : null,
    turns: [{
      index: 1,
      request: "介绍一下项目",
      mode: "agent",
      approval_mode: "automatic",
      reasoning_effort: "auto",
      outcome: terminal ? "answered" : "in_progress",
      summary: terminal ? "Alpha is a Go service." : "",
      summary_stream_id: null,
      changed_files: [],
      project_candidates: candidates,
      project_scope: scope,
      started_at: createdAt,
      completed_at: terminal ? updatedAt : null,
    }],
    verifier_enabled: false,
    plan: null,
    clarification: waiting ? {
      questions: [{
        id: "project_scope",
        prompt: "当前文件夹包含多个项目。你想介绍哪一个?",
        options: [
          {
            id: alphaId,
            label: "alpha",
            description: "Go 项目 · go.mod, README.md",
            recommended: false,
          },
          {
            id: betaId,
            label: "beta",
            description: "Node.js 项目 · package.json, README.md",
            recommended: false,
          },
        ],
      }],
      round: 1,
      purpose: "project_scope",
    } : null,
    pending_approval: null,
    verification: null,
    plan_gate: null,
    step_count: 0,
    repair_cycles: 0,
    context_tokens: 120,
    context_limit: 64_000,
    error: state === "interrupted" ? "The desktop process restarted." : null,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

async function mockShell(page: Page, currentRun: () => ReturnType<typeof projectScopeRun>) {
  await page.route(/\/api\/status$/, (route) => route.fulfill(json({
    version: "test",
    workspace: "/tmp/project-scope-workspace",
    last_workspace: "/tmp/project-scope-workspace",
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    api_key_configured: true,
    connection_verified: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64_000, context_source: "catalog", steps: 30, repair_cycles: 2 },
  })));
  await page.route(/\/api\/provider$/, (route) => route.fulfill(json({
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
  await page.route(/\/api\/projects$/, (route) => route.fulfill(json([])));
  await page.route(/\/api\/runs$/, (route) => route.fulfill(json([currentRun()])));
  await page.route(/\/api\/runs\/project-scope-run\/events(?:\?.*)?$/, (route) => (
    route.fulfill(json([]))
  ));
  await page.route(/\/api\/runs\/project-scope-run\/diff$/, (route) => (
    route.fulfill(json({ diff: "" }))
  ));
  await page.route(/\/api\/runs\/project-scope-run$/, (route) => (
    route.fulfill(json(currentRun()))
  ));
}

test("project selection stays verified, keyboard-visible, and durable across recovery", async ({
  page,
}) => {
  let current = projectScopeRun("awaiting_clarification", "2026-08-31T06:00:01Z");
  await mockShell(page, () => current);
  let answerPayload: Record<string, unknown> | null = null;
  await page.route("**/api/runs/project-scope-run/answers", async (route) => {
    answerPayload = route.request().postDataJSON() as Record<string, unknown>;
    current = projectScopeRun("interrupted", "2026-08-31T06:00:02Z");
    await route.fulfill(json({ accepted: true }, 202));
  });

  await page.goto("/");

  await expect(page.getByRole("heading", { name: "选择要处理的项目" })).toBeVisible();
  await expect(page.getByText("alpha · go.mod · README.md", { exact: true })).toBeVisible();
  await expect(page.getByText("beta · package.json · README.md", { exact: true })).toBeVisible();
  await expect(page.getByPlaceholder("其他答案…")).toHaveCount(0);

  const alpha = page.getByRole("radio", { name: /alpha/ });
  await alpha.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(alpha).toBeFocused();
  const alphaCard = alpha.locator("xpath=ancestor::label");
  await expect.poll(async () => alphaCard.evaluate((card) => {
    const style = getComputedStyle(card);
    return style.outlineStyle !== "none" && Number.parseFloat(style.outlineWidth) > 0;
  })).toBe(true);

  await alpha.check();
  await page.getByRole("button", { name: "使用此项目" }).click();
  await expect.poll(() => answerPayload).toEqual({
    request_id: "project-scope-decision",
    answers: [{ question_id: "project_scope", option_id: alphaId }],
  });

  const scopeBadge = page.locator(".project-scope-badge");
  await expect(scopeBadge).toContainText("项目 alpha");
  await expect(scopeBadge).toHaveAttribute(
    "aria-label",
    "当前轮读取范围限定为 alpha；已读取 README.md",
  );

  current = projectScopeRun("answered", "2026-08-31T06:00:03Z");
  await page.reload();
  await expect(page.locator(".state-badge")).toHaveText("已答复");
  await expect(scopeBadge).toContainText("项目 alpha");
  await expect(scopeBadge).toHaveAttribute(
    "title",
    "当前轮读取范围限定为 alpha；已读取 README.md",
  );
});
