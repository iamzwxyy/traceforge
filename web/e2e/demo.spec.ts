import { expect, test } from "@playwright/test";
import { expectNoWcagViolations } from "./a11y";

test("demo proves a tenant-isolation fix without runtime errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  let openerCalls = 0;
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.route(/\/api\/runs\/[^/]+\/open-workspace$/, async (route) => {
    openerCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 120));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(openerCalls === 1
        ? { supported: true, opened: true, application: "Finder" }
        : { supported: false, opened: false, application: null }),
    });
  });

  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator("textarea")).toHaveValue(/多租户缓存隔离/);
  await expect(page.locator("textarea")).toHaveAttribute("readonly", "");
  await expect(page.getByText("固定演示", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("radio", { name: /手动审批/ })).toBeDisabled();
  await expect(page.getByRole("radio", { name: /自动审批/ })).toBeChecked();
  await expect(page.getByRole("radio", { name: /完全访问（工作区）/ })).toBeDisabled();
  await expect(page.getByRole("group", { name: /本轮思考强度：模型默认，唯一可用/ }))
    .toContainText("模型默认");
  await expect(page.getByText(/当前模型仅提供这个档位/).first()).toBeVisible();
  await expect(page.getByText("与计划模式独立", { exact: true })).toBeVisible();
  await expect(page.getByText("本地就绪", { exact: true })).toBeVisible();
  await expect(page.locator(".sandbox-status")).toContainText(/seatbelt|bubblewrap|仅策略限制/i);

  await page.getByRole("button", { name: "模型设置" }).click();
  await expect(page.locator('input[type="password"]')).toHaveAttribute("type", "password");
  await page.getByText("高级设置", { exact: true }).click();
  await page.getByLabel(/凭证文件路径/).fill("/does/not/exist");
  await page.getByRole("button", { name: "保存设置" }).click();
  const settingsError = page.getByRole("alert");
  await expect(settingsError).toBeVisible();
  await expect(settingsError).toHaveCSS("z-index", "100");
  await expect(settingsError).toContainText("凭证文件不可读");
  expect(consoleErrors).toHaveLength(1);
  expect(consoleErrors[0]).toContain("422 (Unprocessable Entity)");
  consoleErrors.length = 0;
  await page.getByRole("button", { name: "关闭错误提示" }).click();
  await expect(settingsError).toHaveCount(0);
  await page.getByRole("button", { name: "关闭模型设置" }).click();

  await expect(page.getByRole("button", { name: "添加项目" })).toBeDisabled();

  await page.locator("textarea").press("Enter");

  await expect(page.getByRole("heading", { name: /选择会影响具体实现/ })).toBeVisible();
  await page.getByRole("radio", { name: /保留公共 API/ }).check();
  await page.getByRole("button", { name: "继续" }).click();

  await expect(page.getByRole("button", { name: "批准并执行" })).toBeVisible();
  await expect(page.locator(".plan-document")).toContainText("实施计划");
  await expect(page.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", /plan\.md$/);
  await page.getByRole("button", { name: "批准并执行" }).click();

  await expect(page.getByText("本轮已完成", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".reasoning-effort-badge")).toContainText("模型默认");
  await expect(page.getByRole("region", { name: "第 1 轮由编辑工具更改的文件" }))
    .toContainText("src/tenant_cache_api/cache.py");
  await expect(page.getByRole("region", { name: "第 1 轮由编辑工具更改的文件" }))
    .toContainText("tests/test_tenant_isolation.py");
  await page.getByRole("button", { name: "打开目录" }).click();
  await expect(page.getByRole("button", { name: "正在打开" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "打开目录" })).toBeEnabled();
  expect(openerCalls).toBe(1);
  await page.getByRole("button", { name: "打开目录" }).click();
  await expect(page.getByRole("alert")).toContainText("当前系统没有可用的文件管理器");
  await page.getByRole("button", { name: "关闭错误提示" }).click();
  expect(openerCalls).toBe(2);
  await expect(page.getByLabel("继续此任务")).toHaveCount(0);
  await expectNoWcagViolations(page, "completed run");
  await expect(page.getByText("1 项检查通过", { exact: false })).toBeVisible();
  await expect(page.getByText(/\d+(\.\d+)?k? \/ 64k 上下文/)).toBeVisible();
  await expect(page.getByText("实时", { exact: true })).toBeVisible();
  const trace = page.locator(".trace-details");
  await expect(trace).toBeVisible();
  await expect(trace).not.toHaveAttribute("open", "");
  await expect(trace.locator("summary")).toContainText("查看工作记录");
  await trace.locator("summary").click();
  await expect(trace).toHaveAttribute("open", "");
  await expect(trace).toContainText("完成后复核");

  await page.reload();
  await expect(page.getByText("本轮已完成", { exact: true })).toBeVisible();
  await expect(page.getByText("实时", { exact: true })).toBeVisible();
  await expect(page.getByText("1 项检查通过", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "查看证据" }).click();
  const proofDialog = page.getByRole("dialog", { name: "证据包" });
  await expect(proofDialog).toBeVisible();
  await expect(proofDialog.getByText("已证实", { exact: true })).toBeVisible();
  await expect(proofDialog.getByText("稳定证据 SHA-256")).toBeVisible();
  await expect(proofDialog).toContainText("4 passed");
  await expect(proofDialog.getByText("命令沙箱")).toBeVisible();
  await expect(proofDialog.getByText("思考强度")).toBeVisible();
  await expect(proofDialog.getByText(/\d+ 个已强制隔离 · 0 个运行前拦截/)).toBeVisible();
  await expectNoWcagViolations(page, "proof pack dialog");
  await expect(page.getByRole("link", { name: "下载 Markdown" }))
    .toHaveAttribute("href", /proof-pack\.md$/);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "证据包" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看证据" })).toBeFocused();
  await page.getByRole("button", { name: "回滚", exact: true }).click();
  const rollbackDialog = page.getByRole("dialog", { name: "回滚本次运行？" });
  await expect(rollbackDialog).toBeVisible();
  await expect(rollbackDialog).toContainText("用户之后的编辑会作为冲突保留");
  await expect(rollbackDialog.getByRole("button", { name: "取消" })).toBeFocused();
  await expectNoWcagViolations(page, "rollback dialog");
  await page.keyboard.press("Escape");
  await expect(rollbackDialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "回滚", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "回滚", exact: true }).click();
  await page.getByRole("button", { name: "回滚文件" }).click();
  await expect(page.getByText("已回滚", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "新建任务" }).click();
  await expect(page.getByRole("heading", { name: "你想让 TraceForge 帮你做什么？" })).toBeVisible();
  await page.locator(".run-item").first().click();
  await expect(page.getByText("已回滚", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "新建任务" }).click();
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByRole("heading", { name: /选择会影响具体实现/ })).toBeVisible();
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByText("任务已停止", { exact: true }).last()).toBeVisible();
  await expect(page.getByText(/已经写入的文件修改仍保留在工作区/)).toBeVisible();
  await expect(page.getByRole("button", { name: "回滚", exact: true })).toBeVisible();
  await expectNoWcagViolations(page, "stopped run");
  expect(consoleErrors).toEqual([]);
});
