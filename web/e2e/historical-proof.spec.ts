import { expect, test, type Page, type Route } from "@playwright/test";

const createdAt = "2026-08-28T00:00:00Z";

function turn(
  index: number,
  outcome: "answered" | "succeeded",
  summary: string,
) {
  return {
    index,
    request: `第 ${index} 轮请求`,
    mode: "agent",
    approval_mode: "automatic",
    reasoning_effort: "auto",
    outcome,
    summary,
    changed_files: outcome === "succeeded" ? [`turn-${index}.txt`] : [],
    started_at: `2026-08-28T00:0${index}:00Z`,
    completed_at: `2026-08-28T00:0${index}:30Z`,
  };
}

const historyRun = {
  id: "history-run",
  task: "先实现功能，再回答问题",
  workspace: "/tmp/history-run",
  project_id: null,
  state: "answered",
  mode: "agent",
  approval_mode: "automatic",
  reasoning_effort: "auto",
  proof_turn_indexes: [1, 3],
  turns: [
    turn(1, "succeeded", "第一轮完成"),
    turn(2, "answered", "第二轮答复"),
    turn(3, "succeeded", "第三轮完成"),
    turn(4, "answered", "第四轮答复"),
  ],
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
  updated_at: "2026-08-28T00:04:30Z",
};

const answerOnlyRun = {
  ...historyRun,
  id: "answer-only-run",
  task: "纯答复任务",
  workspace: "/tmp/answer-only-run",
  proof_turn_indexes: [],
  turns: [turn(1, "answered", "只回答，不改文件")],
  context_tokens: 100,
  updated_at: "2026-08-28T00:01:30Z",
};

const legacyHoleRun = {
  ...historyRun,
  id: "legacy-hole-run",
  task: "旧数据成功轮没有冻结证据",
  workspace: "/tmp/legacy-hole-run",
  state: "succeeded",
  proof_turn_indexes: [],
  turns: [turn(1, "succeeded", "旧版本曾经成功")],
  context_tokens: 200,
  updated_at: "2026-08-28T00:02:30Z",
};

const currentSuccessWithOlderProof = {
  ...historyRun,
  id: "current-success-with-older-proof",
  task: "本轮成功但只有旧轮冻结证据",
  workspace: "/tmp/current-success-with-older-proof",
  state: "succeeded",
  proof_turn_indexes: [1],
  turns: [
    turn(1, "succeeded", "第一轮有证据"),
    turn(2, "answered", "第二轮答复"),
    turn(3, "succeeded", "第三轮完成但证据暂缺"),
  ],
  context_tokens: 300,
  updated_at: "2026-08-28T00:03:30Z",
};

const rolledBackRun = {
  ...historyRun,
  id: "rolled-back-run",
  task: "已回滚但保留成功证据",
  workspace: "/tmp/rolled-back-run",
  state: "rolled_back",
  proof_turn_indexes: [1],
  turns: [turn(1, "succeeded", "完成后已回滚")],
  context_tokens: 120,
  updated_at: "2026-08-28T00:05:30Z",
};

function proofPack(turnIndex: 1 | 3) {
  const selectedTurns = historyRun.turns.filter((item) => item.index <= turnIndex);
  return {
    schema_version: "traceforge.proof-pack.v2",
    generated_at: selectedTurns.at(-1)?.completed_at,
    run_id: historyRun.id,
    turn_index: turnIndex,
    scope: "cumulative_through_turn",
    event_through_seq: turnIndex * 3,
    task: historyRun.task,
    workspace: historyRun.workspace,
    project_id: null,
    mode: "agent",
    turns: selectedTurns,
    state: "succeeded",
    proof_status: "proven",
    plan: null,
    plan_gate: null,
    changed_files: [`turn-${turnIndex}.txt`],
    diff: `DIFF-TURN-${turnIndex}`,
    diff_source: "completion_event",
    diff_sha256: `turn-${turnIndex}-diff-sha`,
    checks_fresh: true,
    verification: {
      verdict: "pass",
      summary: `第 ${turnIndex} 轮已复核`,
      findings: [],
      checked_at: selectedTurns.at(-1)?.completed_at,
    },
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
    event_count: turnIndex * 3,
    event_chain_sha256: `turn-${turnIndex}-event-sha`,
    step_count: turnIndex === 1 ? 2 : 7,
    repair_cycles: 0,
    created_at: historyRun.created_at,
    updated_at: selectedTurns.at(-1)?.completed_at,
    evidence_sha256: `EVIDENCE-TURN-${turnIndex}`,
    artifact_sha256: "b".repeat(64),
  };
}

function json(body: unknown) {
  return {
    status: 200,
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

async function afterBrowserCommit(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function mockHistoryRun(
  page: Page,
  respondWithProof: (route: Route, turnIndex: 1 | 3) => Promise<void> | void,
) {
  await page.route("**/api/status", (route) => route.fulfill(json({
    version: "test",
    workspace: historyRun.workspace,
    last_workspace: historyRun.workspace,
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    api_key_configured: true,
    connection_verified: true,
    suggested_task: null,
    mode: "standard",
    sandbox: { backend: "seatbelt", enforced: true, detail: "test" },
    limits: { context: 64_000, context_source: "catalog", steps: 30, repair_cycles: 2 },
  })));
  await page.route("**/api/runs", (route) => route.fulfill(json([
    historyRun,
    currentSuccessWithOlderProof,
    rolledBackRun,
    legacyHoleRun,
    answerOnlyRun,
  ])));
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
  await page.route(/\/api\/runs\/(history-run|current-success-with-older-proof|rolled-back-run|legacy-hole-run|answer-only-run)$/, (route) => {
    const url = route.request().url();
    const selectedRun = url.endsWith("/history-run")
      ? historyRun
      : url.endsWith("/current-success-with-older-proof")
        ? currentSuccessWithOlderProof
      : url.endsWith("/rolled-back-run")
        ? rolledBackRun
      : url.endsWith("/legacy-hole-run")
        ? legacyHoleRun
        : answerOnlyRun;
    return route.fulfill(json(selectedRun));
  });
  await page.route(/\/api\/runs\/(history-run|current-success-with-older-proof|rolled-back-run|legacy-hole-run|answer-only-run)\/events\?/, (route) => (
    route.fulfill(json([]))
  ));
  await page.route(/\/api\/runs\/(history-run|current-success-with-older-proof|rolled-back-run|legacy-hole-run|answer-only-run)\/diff$/, (route) => (
    route.fulfill(json({ diff: "" }))
  ));
  await page.route(/\/api\/runs\/history-run\/proof-pack\?turn_index=(1|3)$/, (route) => {
    const match = new URL(route.request().url()).searchParams.get("turn_index");
    return respondWithProof(route, match === "1" ? 1 : 3);
  });
  await page.route(/\/api\/runs\/rolled-back-run\/proof-pack\?turn_index=1$/, (route) => (
    route.fulfill(json({
      ...proofPack(1),
      run_id: rolledBackRun.id,
      task: rolledBackRun.task,
      workspace: rolledBackRun.workspace,
      turns: rolledBackRun.turns,
      artifact_sha256: "c".repeat(64),
    }))
  ));
}

test("an answered follow-up keeps historical proof and switches successful turns", async ({ page }) => {
  await mockHistoryRun(page, (route, turnIndex) => route.fulfill(json(proofPack(turnIndex))));

  await page.goto("/");
  await expect(page.getByText("本轮已直接答复", { exact: true })).toBeVisible();
  await expect(page.getByText("第 3 轮已证实", { exact: true })).toBeVisible();
  await expect(page.getByText("查看截至该轮冻结的累计证据；不代表当前轮已完成。", { exact: true }))
    .toBeVisible();
  await expect(page.getByText("本轮已完成", { exact: true })).toHaveCount(0);

  await page.setViewportSize({ width: 320, height: 568 });
  await page.getByRole("button", { name: "查看历史证据" }).click();
  let dialog = page.getByRole("dialog", { name: "截至第 3 轮的累计证据包" });
  await expect(dialog).toContainText("EVIDENCE-TURN-3");
  await expect(dialog.getByText("第 3 轮 · 7 个工具动作 · 0 轮修复", { exact: true }))
    .toBeVisible();
  const turnPicker = dialog.getByRole("combobox", { name: "选择证据轮次" });
  await expect(turnPicker).toHaveValue("3");
  await expect.poll(() => page.evaluate(() => (
    document.documentElement.scrollWidth <= window.innerWidth
  ))).toBe(true);
  const footerCopy = dialog.getByText(
    "这是截至所选成功轮冻结的累计快照；后续轮次不会改写它。",
    { exact: true },
  );
  await expect(footerCopy).toHaveCSS("white-space", "normal");
  expect(await footerCopy.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  await expect(dialog.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", "/api/runs/history-run/proof-pack.md?turn_index=3");

  await turnPicker.selectOption("1");
  dialog = page.getByRole("dialog", { name: "截至第 1 轮的累计证据包" });
  await expect(dialog).toContainText("EVIDENCE-TURN-1");
  await expect(dialog.getByText("第 1 轮 · 2 个工具动作 · 0 轮修复", { exact: true }))
    .toBeVisible();
  await expect(dialog.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", "/api/runs/history-run/proof-pack.md?turn_index=1");

  await dialog.getByRole("combobox", { name: "选择证据轮次" }).selectOption("3");
  dialog = page.getByRole("dialog", { name: "截至第 3 轮的累计证据包" });
  await expect(dialog).toContainText("EVIDENCE-TURN-3");
  await page.getByRole("button", { name: "关闭证据包" }).click();

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.locator(".run-item").filter({ hasText: "纯答复任务" }).click();
  await expect(page.getByRole("heading", { name: "纯答复任务" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看历史证据" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看证据" })).toHaveCount(0);

  await page.locator(".run-item").filter({ hasText: "本轮成功但只有旧轮冻结证据" }).click();
  await expect(page.getByText("本轮已完成", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 768 });
  const unavailable = page.getByText("暂无冻结证据", { exact: true });
  await expect(unavailable).toBeVisible();
  expect(await unavailable.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  await expect(page.getByText("第 1 轮已证实", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看证据" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看历史证据" })).toBeVisible();

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.locator(".run-item").filter({ hasText: "已回滚但保留成功证据" }).click();
  await expect(page.getByText("本轮已完成", { exact: true })).toHaveCount(0);
  await expect(page.getByText("第 1 轮已证实", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看历史证据" }).click();
  const rolledBackProof = page.getByRole("dialog", { name: "截至第 1 轮的累计证据包" });
  await expect(rolledBackProof).toContainText("已回滚但保留成功证据");
  await expect(rolledBackProof.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", "/api/runs/rolled-back-run/proof-pack.md?turn_index=1");
  await page.getByRole("button", { name: "关闭证据包" }).click();

  await page.locator(".run-item").filter({ hasText: "旧数据成功轮没有冻结证据" }).click();
  await expect(page.getByRole("heading", { name: "旧数据成功轮没有冻结证据" }))
    .toBeVisible();
  await expect(page.getByText("本轮已完成", { exact: true })).toBeVisible();
  await expect(page.getByText(/暂无冻结证据/)).toBeVisible();
  await expect(page.getByRole("button", { name: "查看历史证据" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看证据" })).toHaveCount(0);
});

test("a reversed proof response cannot replace the newly selected turn", async ({ page }) => {
  const delayedTurnOne = deferred();
  let turnOneRequested = false;
  await mockHistoryRun(page, async (route, turnIndex) => {
    if (turnIndex === 1) {
      turnOneRequested = true;
      await delayedTurnOne.promise;
    }
    await route.fulfill(json(proofPack(turnIndex)));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "查看历史证据" }).click();
  let dialog = page.getByRole("dialog", { name: "截至第 3 轮的累计证据包" });
  await expect(dialog).toContainText("EVIDENCE-TURN-3");

  await dialog.getByRole("combobox", { name: "选择证据轮次" }).selectOption("1");
  await expect.poll(() => turnOneRequested).toBe(true);
  dialog = page.getByRole("dialog", { name: "截至第 1 轮的累计证据包" });
  await expect(dialog.getByText("正在读取持久化证据…")).toBeVisible();

  await dialog.getByRole("combobox", { name: "选择证据轮次" }).selectOption("3");
  dialog = page.getByRole("dialog", { name: "截至第 3 轮的累计证据包" });
  await expect(dialog).toContainText("EVIDENCE-TURN-3");
  await expect(dialog.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", "/api/runs/history-run/proof-pack.md?turn_index=3");

  const lateResponse = page.waitForResponse(
    /\/api\/runs\/history-run\/proof-pack\?turn_index=1$/,
  );
  delayedTurnOne.resolve();
  await (await lateResponse).finished();
  await afterBrowserCommit(page);
  await expect(dialog).toContainText("EVIDENCE-TURN-3");
  await expect(dialog).not.toContainText("EVIDENCE-TURN-1");
  await expect(dialog.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", "/api/runs/history-run/proof-pack.md?turn_index=3");
});

test("a proof response for the wrong turn is rejected", async ({ page }) => {
  let requestCount = 0;
  await mockHistoryRun(page, (route) => {
    requestCount += 1;
    return route.fulfill(json(proofPack(requestCount === 1 ? 1 : 3)));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "查看历史证据" }).click();
  const dialog = page.getByRole("dialog", { name: "截至第 3 轮的累计证据包" });
  await expect(dialog.getByRole("alert"))
    .toContainText("证据包响应与请求的轮次不匹配");
  await expect(dialog.getByRole("button", { name: "重试" })).toBeVisible();
  await expect(dialog).not.toContainText("EVIDENCE-TURN-1");
  await expect(dialog.locator("a").filter({ hasText: "下载 Markdown" }))
    .not.toHaveAttribute("href");
  await dialog.getByRole("button", { name: "重试" }).click();
  await expect.poll(() => requestCount).toBe(2);
  await expect(dialog).toContainText("EVIDENCE-TURN-3");
  await expect(dialog.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", "/api/runs/history-run/proof-pack.md?turn_index=3");
});
