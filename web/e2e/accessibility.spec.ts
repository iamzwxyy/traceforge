import { expect, test } from "@playwright/test";
import { expectNoWcagViolations } from "./a11y";

test("new-task and setup dialogs meet the automated WCAG A/AA baseline", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New run" }).click();
  await expect(page.getByRole("heading", { name: "What should TraceForge prove?" })).toBeVisible();
  await expectNoWcagViolations(page, "new task");

  const settingsTrigger = page.getByRole("button", { name: "Model settings" });
  await settingsTrigger.click();
  await expect(page.getByLabel("Model", { exact: true })).toBeFocused();
  await expectNoWcagViolations(page, "model settings dialog");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Connection settings" })).toHaveCount(0);
  await expect(settingsTrigger).toBeFocused();

  const browseTrigger = page.getByRole("button", { name: "Browse" });
  await browseTrigger.click();
  await expect(page.getByLabel("Directory path")).toBeFocused();
  await expectNoWcagViolations(page, "directory dialog");
  await page.getByRole("button", { name: "Close directory browser" }).focus();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Choose this directory" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Choose a directory" })).toHaveCount(0);
  await expect(browseTrigger).toBeFocused();

  await page.getByRole("button", { name: "Project", exact: true }).click();
  const projectTrigger = page.getByRole("button", { name: "New project" });
  await projectTrigger.click();
  await expect(page.getByLabel("Project name")).toBeFocused();
  await expectNoWcagViolations(page, "project dialog");
  const projectDialog = page.getByRole("dialog", { name: "Add a project" });
  const nestedBrowse = projectDialog.getByRole("button", { name: "Browse" });
  await nestedBrowse.click();
  await expect(page.getByRole("dialog", { name: "Choose a directory" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Choose a directory" })).toHaveCount(0);
  await expect(projectDialog).toBeVisible();
  await expect(nestedBrowse).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(projectDialog).toHaveCount(0);
  await expect(projectTrigger).toBeFocused();
});
