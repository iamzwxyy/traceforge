import { expect, test } from "@playwright/test";

test("demo proves a tenant-isolation fix without runtime errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/");
  await expect(page.locator("textarea")).toHaveValue(/multi-tenant cache isolation/);
  await expect(page.locator(".sandbox-status")).toContainText(/seatbelt|bubblewrap|Policy only/i);

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
  await expect(page.getByText("4 passed", { exact: false }).first()).toBeVisible();
  await expect(page.getByText(/\d+(\.\d+)?k? \/ 64k ctx/)).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Planning & decisions/ }))
    .toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: /Build & checks/ }))
    .toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: /Independent verification/ }))
    .toHaveAttribute("aria-expanded", "true");
  await page.getByRole("button", { name: "Proof Pack" }).click();
  await expect(page.getByRole("heading", { name: "Proof Pack" })).toBeVisible();
  await expect(page.getByText("proven", { exact: true })).toBeVisible();
  await expect(page.getByText("STABLE EVIDENCE SHA-256")).toBeVisible();
  await expect(page.getByText("COMMAND SANDBOX")).toBeVisible();
  await expect(page.getByText(/\d+ enforced · 0 blocked before run/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Download Markdown" }))
    .toHaveAttribute("href", /proof-pack\.md$/);
  expect(consoleErrors).toEqual([]);
});
