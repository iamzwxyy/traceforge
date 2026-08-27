import { expect, test } from "@playwright/test";
import { expectNoWcagViolations } from "./a11y";

test("demo proves a tenant-isolation fix without runtime errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/");
  await expect(page.locator("textarea")).toHaveValue(/multi-tenant cache isolation/);
  await expect(page.getByText("Local ready", { exact: true })).toBeVisible();
  await expect(page.locator(".sandbox-status")).toContainText(/seatbelt|bubblewrap|Policy only/i);

  await page.getByRole("button", { name: "Model settings" }).click();
  await page.getByLabel(/Credential file path/).fill("/does/not/exist");
  await page.getByRole("button", { name: "Save settings" }).click();
  const settingsError = page.getByRole("alert");
  await expect(settingsError).toBeVisible();
  await expect(settingsError).toHaveCSS("z-index", "100");
  await expect(settingsError).toContainText("Credential file is not readable");
  expect(consoleErrors).toHaveLength(1);
  expect(consoleErrors[0]).toContain("422 (Unprocessable Entity)");
  consoleErrors.length = 0;
  await page.getByRole("button", { name: "Dismiss error" }).click();
  await expect(settingsError).toHaveCount(0);
  await page.getByRole("button", { name: "Close model settings" }).click();

  await page.getByRole("button", { name: "Browse" }).click();
  await expect(page.getByRole("heading", { name: "Choose a directory" })).toBeVisible();
  await page.getByRole("button", { name: "Choose this directory" }).click();

  await page.getByRole("button", { name: "Project", exact: true }).click();
  await page.getByRole("button", { name: "New project" }).click();
  await page.getByLabel("Project name").fill("Demo workspace");
  await page.getByRole("button", { name: "Add project" }).click();
  await expect(page.getByRole("combobox", { name: "Project" })).toHaveValue(/.+/);
  await page.getByRole("button", { name: "Direct task" }).click();

  await page.getByRole("button", { name: "Start run" }).click();

  await expect(page.getByRole("heading", { name: /decisions change/ })).toBeVisible();
  await page.getByRole("radio", { name: /Preserve public API/ }).check();
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("button", { name: "Approve & build" })).toBeVisible();
  await page.getByRole("button", { name: "Approve & build" }).click();

  await expect(page.getByRole("heading", { name: "Work proven, not merely reported" }))
    .toBeVisible({ timeout: 15_000 });
  await expectNoWcagViolations(page, "completed run");
  await expect(page.getByText("4 passed", { exact: false }).first()).toBeVisible();
  await expect(page.getByText(/\d+(\.\d+)?k? \/ 64k ctx/)).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Planning & decisions/ }))
    .toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: /Build & checks/ }))
    .toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: /Independent verification/ }))
    .toHaveAttribute("aria-expanded", "true");

  await page.reload();
  await expect(page.getByRole("heading", { name: "Work proven, not merely reported" }))
    .toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(page.getByText("4 passed", { exact: false }).first()).toBeVisible();
  await page.getByRole("button", { name: "Proof Pack" }).click();
  await expect(page.getByRole("heading", { name: "Proof Pack" })).toBeVisible();
  await expect(page.getByText("proven", { exact: true })).toBeVisible();
  await expect(page.getByText("STABLE EVIDENCE SHA-256")).toBeVisible();
  await expect(page.getByText("COMMAND SANDBOX")).toBeVisible();
  await expect(page.getByText(/\d+ enforced · 0 blocked before run/)).toBeVisible();
  await expectNoWcagViolations(page, "proof pack dialog");
  await expect(page.getByRole("link", { name: "Download Markdown" }))
    .toHaveAttribute("href", /proof-pack\.md$/);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Proof Pack" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Proof Pack" })).toBeFocused();
  await page.getByRole("button", { name: "Rollback", exact: true }).click();
  const rollbackDialog = page.getByRole("dialog", { name: "Rollback this run?" });
  await expect(rollbackDialog).toBeVisible();
  await expect(rollbackDialog).toContainText("Later user edits are preserved as conflicts");
  await expect(rollbackDialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await expectNoWcagViolations(page, "rollback dialog");
  await page.keyboard.press("Escape");
  await expect(rollbackDialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Rollback", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Rollback", exact: true }).click();
  await page.getByRole("button", { name: "Rollback files" }).click();
  await expect(page.getByText("Rolled back", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "New run" }).click();
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page.getByRole("heading", { name: /decisions change/ })).toBeVisible();
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByText("Run stopped", { exact: true }).last()).toBeVisible();
  await expect(page.getByText(/Any file changes already made remain in the workspace/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Rollback", exact: true })).toBeVisible();
  await expectNoWcagViolations(page, "stopped run");
  expect(consoleErrors).toEqual([]);
});
