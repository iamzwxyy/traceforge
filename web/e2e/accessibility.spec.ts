import { expect, test } from "@playwright/test";
import { expectNoWcagViolations } from "./a11y";

test("new-task and setup dialogs meet the automated WCAG A/AA baseline", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建任务" }).click();
  await expect(page.getByRole("heading", { name: "你希望 TraceForge 完成并证明什么？" })).toBeVisible();
  await expectNoWcagViolations(page, "new task");

  const settingsTrigger = page.getByRole("button", { name: "模型设置" });
  await settingsTrigger.click();
  await expect(page.getByLabel("模型", { exact: true })).toBeFocused();
  await expectNoWcagViolations(page, "model settings dialog");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "连接设置" })).toHaveCount(0);
  await expect(settingsTrigger).toBeFocused();

  await expect(page.getByRole("button", { name: "添加项目" })).toBeDisabled();

  await page.setViewportSize({ width: 390, height: 768 });
  const historyTrigger = page.getByRole("button", { name: "任务与项目" });
  await historyTrigger.click();
  await expectNoWcagViolations(page, "task history drawer");
  await page.getByRole("button", { name: "关闭任务与项目" }).focus();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "新建任务" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "任务与项目" })).toHaveCount(0);
  await expect(historyTrigger).toBeFocused();
});
