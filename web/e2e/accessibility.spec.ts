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

  const browseTrigger = page.getByRole("button", { name: "浏览" });
  await browseTrigger.click();
  await expect(page.getByLabel("目录路径")).toBeFocused();
  await expectNoWcagViolations(page, "directory dialog");
  await page.getByRole("button", { name: "关闭目录浏览器" }).focus();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "选择当前目录" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "选择目录" })).toHaveCount(0);
  await expect(browseTrigger).toBeFocused();

  await page.getByRole("button", { name: "项目", exact: true }).click();
  const projectTrigger = page.getByRole("button", { name: "新建项目" });
  await projectTrigger.click();
  await expect(page.getByLabel("项目名称")).toBeFocused();
  await expectNoWcagViolations(page, "project dialog");
  const projectDialog = page.getByRole("dialog", { name: "添加项目" });
  const nestedBrowse = projectDialog.getByRole("button", { name: "浏览" });
  await nestedBrowse.click();
  await expect(page.getByRole("dialog", { name: "选择目录" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "选择目录" })).toHaveCount(0);
  await expect(projectDialog).toBeVisible();
  await expect(nestedBrowse).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(projectDialog).toHaveCount(0);
  await expect(projectTrigger).toBeFocused();
});
