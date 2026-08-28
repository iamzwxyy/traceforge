import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const repository = fileURLToPath(new URL("../..", import.meta.url));
const videoDirectory = new URL("../../artifacts/video/raw/", import.meta.url);
const baseURL = "http://127.0.0.1:8910";

await mkdir(videoDirectory, { recursive: true });
const server = spawn("uv", ["run", "traceforge", "demo", "--port", "8910"], {
  cwd: repository,
  stdio: ["ignore", "pipe", "pipe"],
  detached: process.platform !== "win32",
});
let serverLogs = "";
for (const stream of [server.stdout, server.stderr]) {
  stream.on("data", (chunk) => {
    serverLogs = `${serverLogs}${chunk}`.slice(-8_000);
  });
}

async function waitForServer() {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseURL}/healthz`);
      if (response.ok) return;
    } catch {
      // The server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Demo server did not start in time\n${serverLogs}`);
}

async function stopServer() {
  if (server.exitCode !== null || !server.pid) return;
  const stop = (signal) => {
    if (server.exitCode !== null || !server.pid) return;
    if (process.platform === "win32") server.kill(signal);
    else process.kill(-server.pid, signal);
  };
  stop("SIGTERM");
  await Promise.race([
    once(server, "exit"),
    new Promise((resolve) => setTimeout(resolve, 5_000)),
  ]);
  if (server.exitCode === null) {
    stop("SIGKILL");
    await once(server, "exit");
  }
}

async function caption(page, text) {
  await page.evaluate((nextText) => {
    let node = document.querySelector("[data-video-caption]");
    if (!node) {
      node = document.createElement("div");
      node.dataset.videoCaption = "true";
      Object.assign(node.style, {
        position: "fixed",
        zIndex: "9999",
        right: "24px",
        bottom: "22px",
        left: "24px",
        padding: "14px 18px",
        color: "#edf8f3",
        background: "rgba(7, 13, 18, 0.92)",
        border: "1px solid rgba(118, 228, 184, 0.42)",
        borderRadius: "10px",
        boxShadow: "0 16px 50px rgba(0, 0, 0, 0.45)",
        font: "600 18px/1.45 -apple-system, BlinkMacSystemFont, sans-serif",
        textAlign: "center",
        pointerEvents: "none",
      });
      document.body.append(node);
    }
    node.textContent = nextText;
  }, text);
}

let browser;
let context;
try {
  await waitForServer();
  browser = await chromium.launch({ channel: "chrome", headless: true });
  context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    recordVideo: { dir: fileURLToPath(videoDirectory), size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  await page.goto(baseURL);
  await caption(page, "TraceForge：先澄清、再计划、以证据证明完成");
  await page.waitForTimeout(3500);

  await page.getByRole("button", { name: "开始任务" }).click();
  await page.getByRole("heading", { name: /选择会影响具体实现/ }).waitFor();
  await caption(page, "复杂需求不会静默猜测：提供互斥选项与推荐答案");
  await page.waitForTimeout(4500);
  await page.getByRole("radio", { name: /保留公共 API/ }).check();
  await page.waitForTimeout(1200);
  await page.getByRole("button", { name: "继续" }).click();

  await page.getByRole("button", { name: "批准并执行" }).waitFor();
  await caption(page, "计划是完成契约；批准前没有任何文件修改");
  await page.waitForTimeout(5000);
  await page.getByRole("button", { name: "批准并执行" }).click();
  await caption(page, "Builder 通过受限工具修复真实的跨租户缓存串读");

  await page.getByText("本轮已完成", { exact: true }).waitFor({ timeout: 20_000 });
  await page.waitForTimeout(3000);
  await page.getByRole("button", { name: "任务详情" }).click();
  await page.getByRole("button", { name: "差异" }).click();
  await caption(page, "Diff 来自文件快照；回滚会保护用户后续修改");
  await page.waitForTimeout(5000);

  await page.getByRole("button", { name: "计划" }).click();
  await caption(page, "验收命令真实执行：4 项测试通过，退出码与输出持久化");
  await page.waitForTimeout(5000);

  await page.getByRole("button", { name: "验证" }).click();
  await caption(page, "独立只读 Verifier 审查任务、计划、Diff 与测试证据");
  await page.waitForTimeout(5000);

  await page.getByRole("button", { name: "时间线" }).click();
  await caption(page, "模型只提出动作；状态机、权限、恢复与终止逻辑全部自研");
  await page.waitForTimeout(6000);
  await caption(page, "TraceForge —— 对话优先，证据按需展开");
  await page.waitForTimeout(3500);
} finally {
  await context?.close();
  await browser?.close();
  await stopServer();
}
