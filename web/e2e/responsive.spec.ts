import { expect, test } from "@playwright/test";

const viewports = [
  { width: 1366, sidebar: true, inspector: true },
  { width: 1024, sidebar: true, inspector: true },
  { width: 980, sidebar: true, inspector: false },
  { width: 680, sidebar: false, inspector: false },
  { width: 390, sidebar: false, inspector: false },
];

test("new-task layout stays operable without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: viewports[0].width, height: 768 });
  await page.goto("/");
  const heading = page.getByRole("heading", { name: "你希望 TraceForge 完成并证明什么？" });
  await page.getByRole("button", { name: "新建任务" }).click();
  await expect(heading).toBeVisible();
  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: 768 });
    await expect(heading).toBeVisible();
    if (viewport.sidebar) await expect(page.locator(".sidebar")).toBeVisible();
    else await expect(page.locator(".sidebar")).toBeHidden();
    if (viewport.inspector) await expect(page.locator(".inspector")).toBeVisible();
    else await expect(page.locator(".inspector")).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
      .toBe(true);
  }

  const browse = page.getByRole("button", { name: "浏览" });
  await browse.click();
  const dialog = page.getByRole("dialog", { name: "选择目录" });
  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.keyboard.press("Escape");
  await expect(browse).toBeFocused();
});
