import { expect, test } from "@playwright/test";

test("demo proves a tenant-isolation fix without runtime errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/");
  await expect(page.getByRole("textbox")).toHaveValue(/multi-tenant cache isolation/);
  await page.getByRole("button", { name: "Start run" }).click();

  await expect(page.getByRole("heading", { name: /decisions change/ })).toBeVisible();
  await page.getByRole("radio", { name: /Preserve public API/ }).check();
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("button", { name: "Approve & build" })).toBeVisible();
  await page.getByRole("button", { name: "Approve & build" }).click();

  await expect(page.getByRole("heading", { name: "Work proven, not merely reported" }))
    .toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("4 passed", { exact: false }).first()).toBeVisible();
  await expect(page.getByText(/\d+(\.\d+)?k? \/ 64k ctx/)).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  expect(consoleErrors).toEqual([]);
});
