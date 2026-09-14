// Verifies period-level auto-generation defaults in the program editor.
// All APIs are mocked; this test does not touch any local or remote database.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15179;
const base = `http://127.0.0.1:${port}`;
const server = spawn(
  process.execPath,
  [
    "frontend/node_modules/vite/bin/vite.js",
    "frontend",
    "--host",
    "127.0.0.1",
    "--port",
    String(port),
    "--strictPort",
  ],
  { cwd: root, stdio: "ignore" },
);

let postedProgram;

async function setup(context) {
  await context.addInitScript(() => {
    localStorage.setItem("locale", "zh-CN");
    localStorage.setItem("theme", "latte");
  });
  await context.route("https://**/*", (route) => route.abort());
  await context.route("**/api/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const json = (data) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    if (pathname === "/api/auth/session") return json({ authenticated: true });
    if (pathname === "/api/programs") return json({ programs: [] });
    if (pathname === "/api/admin/programs" && request.method() === "POST") {
      postedProgram = request.postDataJSON();
      return json({
        program: {
          id: "single-1",
          ...postedProgram,
          status: "ongoing",
          episode_count: 1,
          occurrences: [],
        },
      });
    }
    if (pathname === "/api/admin/programs/single-1/occurrences") return json({ occurrences: [] });
    return json({});
  });
}

(async () => {
  let browser;
  try {
    for (let i = 0; i < 100; i++) {
      try {
        if ((await fetch(base)).ok) break;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({
      headless: true,
      executablePath:
        process.env.CHROME_PATH ||
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
    await setup(context);
    const page = await context.newPage();

    await page.goto(`${base}/admin/programs`);
    await page.getByRole("button", { name: "新建节目" }).first().click();
    const periodCard = page.locator(".program-period-card").first();
    await periodCard.waitFor();
    assert.equal(await page.locator(".program-editor-danger-actions").count(), 0);
    const autoToggle = periodCard.locator(".period-auto-toggle input");
    assert.equal(await autoToggle.count(), 1);
    assert.equal(await autoToggle.isChecked(), true);

    await periodCard.getByRole("button", { name: "逐期设置" }).click();
    assert.equal(await autoToggle.count(), 0);
    assert.equal(await periodCard.locator(".period-time-field").count(), 1);
    await periodCard.locator('input[placeholder="选择时间"]').fill("20:00");
    assert.equal(await periodCard.locator('input[placeholder="选择时间"]').inputValue(), "20:00");
    assert.match(await periodCard.innerText(), /逐期设置不代表月更/);

    await periodCard.getByRole("button", { name: "月更" }).click();
    const timeBox = await periodCard.locator(".period-time-field").boundingBox();
    const timezoneBox = await periodCard.locator(".period-timezone-field").boundingBox();
    const endBox = await periodCard.locator(".period-end-field").boundingBox();
    const fixedAutoBox = await periodCard.locator(".period-auto-toggle").boundingBox();
    assert.ok(Math.abs(timeBox.y - timezoneBox.y) < 1);
    assert.ok(fixedAutoBox.y > endBox.y + endBox.height);

    await periodCard.getByRole("button", { name: "无规律" }).click();
    assert.equal(await autoToggle.count(), 1);
    assert.equal(await autoToggle.isChecked(), true);
    assert.equal(await periodCard.getByText("播出时间", { exact: true }).count(), 0);
    assert.match(await periodCard.locator(".period-auto-toggle").innerText(), /每月 1 日/);

    await periodCard.getByRole("button", { name: "单次" }).click();
    assert.equal(await autoToggle.count(), 0);
    assert.equal(await periodCard.getByText("播出时间", { exact: true }).count(), 1);
    assert.match(await periodCard.innerText(), /自动生成一条单集/);

    await page.getByLabel("节目名称").fill("单次保存测试");
    await periodCard.locator('input[placeholder="选择日期"]').fill("2026-09-20");
    const timeInput = periodCard.locator('input[placeholder="选择时间"]');
    await timeInput.fill("20:00");
    await timeInput.press("Enter");
    assert.equal(await page.getByRole("button", { name: "添加节目" }).count(), 1);
    await page.getByRole("button", { name: "添加节目" }).click();
    await page.waitForTimeout(100);
    assert.equal(postedProgram.periods[0].frequency, "single");
    assert.equal(postedProgram.periods[0].auto_generate, true);
    assert.equal(postedProgram.periods[0].start_date, "2026-09-20");
    assert.equal(postedProgram.periods[0].schedule_time, "20:00");
    console.log("PASS: monthly modes, manual per-episode mode, and implicit single generation.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
