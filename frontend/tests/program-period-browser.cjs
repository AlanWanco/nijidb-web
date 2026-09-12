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

async function setup(context) {
  await context.addInitScript(() => {
    localStorage.setItem("locale", "zh-CN");
    localStorage.setItem("theme", "latte");
  });
  await context.route("https://**/*", (route) => route.abort());
  await context.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const json = (data) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    if (pathname === "/api/auth/session") return json({ authenticated: true });
    if (pathname === "/api/programs") return json({ programs: [] });
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
    const autoToggle = periodCard.locator(".period-auto-toggle input");
    assert.equal(await autoToggle.isChecked(), true);

    await periodCard.getByRole("button", { name: "逐期设置" }).click();
    assert.equal(await autoToggle.isChecked(), false);
    assert.match(
      await periodCard.locator(".period-auto-toggle").innerText(),
      /关闭后不会按月生成占位单集/,
    );

    await autoToggle.check();
    assert.equal(await autoToggle.isChecked(), true);
    assert.match(await periodCard.locator(".period-auto-toggle").innerText(), /按月生成单集/);

    await periodCard.getByRole("button", { name: "单次" }).click();
    assert.equal(await autoToggle.isChecked(), false);
    await autoToggle.check();
    assert.match(
      await periodCard.locator(".period-auto-toggle").innerText(),
      /开启后只生成时期开始日的一期/,
    );
    console.log("PASS: period-level auto-generation defaults and explicit enabling.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
