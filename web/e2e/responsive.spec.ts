import { expect, test } from "@playwright/test";

const viewports = [
  { width: 1366, sidebar: true, inspector: false },
  { width: 1024, sidebar: true, inspector: false },
  { width: 980, sidebar: true, inspector: false },
  { width: 680, sidebar: false, inspector: false },
  { width: 390, sidebar: false, inspector: false },
];

test("new-task layout stays operable without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: viewports[0].width, height: 768 });
  await page.goto("/");
  const heading = page.getByRole("heading", { name: "你想让 TraceForge 帮你做什么？" });
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

  await page.setViewportSize({ width: 390, height: 768 });
  const history = page.getByRole("button", { name: "任务与项目" });
  await history.click();
  const historyDrawer = page.getByRole("dialog", { name: "任务与项目" });
  await expect(historyDrawer).toBeVisible();
  await expect(page.getByRole("button", { name: "关闭任务与项目" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(historyDrawer).toHaveCount(0);
  await expect(history).toBeFocused();

  await page.setViewportSize({ width: 980, height: 768 });
  const details = page.getByRole("button", { name: "任务详情" });
  await details.click();
  const detailsDrawer = page.getByRole("dialog", { name: "任务详情" });
  await expect(detailsDrawer).toBeVisible();
  await expect(page.getByRole("button", { name: "关闭任务详情" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(detailsDrawer).toHaveCount(0);
  await expect(details).toBeFocused();

  await page.setViewportSize({ width: 1366, height: 768 });
  await expect(details).toHaveAttribute("aria-expanded", "false");
  await details.click();
  await expect(page.locator(".inspector")).toBeVisible();
  const leftHandle = page.getByRole("separator", { name: "调整任务侧栏宽度" });
  const rightHandle = page.getByRole("separator", { name: "调整详情侧栏宽度" });
  const leftBefore = Number(await leftHandle.getAttribute("aria-valuenow"));
  const leftBox = await leftHandle.boundingBox();
  expect(leftBox).not.toBeNull();
  await page.mouse.move(leftBox!.x + leftBox!.width / 2, leftBox!.y + 80);
  await page.mouse.down();
  await page.mouse.move(leftBox!.x + leftBox!.width / 2 + 32, leftBox!.y + 80);
  await page.mouse.up();
  await expect.poll(async () => Number(await leftHandle.getAttribute("aria-valuenow")))
    .toBeGreaterThan(leftBefore);

  const rightBefore = Number(await rightHandle.getAttribute("aria-valuenow"));
  await rightHandle.focus();
  await rightHandle.press("ArrowLeft");
  await expect.poll(async () => Number(await rightHandle.getAttribute("aria-valuenow")))
    .toBeGreaterThan(rightBefore);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);

  const persistedLeft = Number(await leftHandle.getAttribute("aria-valuenow"));
  const persistedRight = Number(await rightHandle.getAttribute("aria-valuenow"));
  await expect.poll(() => page.evaluate(() => localStorage.getItem("traceforge:layout:v1:right-collapsed")))
    .toBe("false");
  await page.reload();
  await expect(page.getByRole("separator", { name: "调整任务侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", String(persistedLeft));
  await expect(page.getByRole("separator", { name: "调整详情侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", String(persistedRight));

  await page.evaluate(() => {
    localStorage.setItem("traceforge:layout:v1:left-width", "99999");
    localStorage.setItem("traceforge:layout:v1:right-width", "not-a-number");
  });
  await page.reload();
  await expect(page.getByRole("separator", { name: "调整任务侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", "276");
  await expect(page.getByRole("separator", { name: "调整详情侧栏宽度" }))
    .toHaveAttribute("aria-valuenow", "390");
});
