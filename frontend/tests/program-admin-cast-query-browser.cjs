// Verifies the admin program list reads and writes the cast query parameter.
// All APIs are mocked; this test does not touch any local or remote database.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15180;
const base = `http://127.0.0.1:${port}`;
const programs = [
  {
    id: "ayumu",
    title: "大西亜玖璃的节目",
    category: "personal",
    format: "video",
    platform: "network",
    delivery: "recorded",
    status: "ongoing",
    people: ["大西亜玖璃"],
    periods: [],
    episode_count: 1,
  },
  {
    id: "setsuna",
    title: "相良茉優的节目",
    category: "personal",
    format: "video",
    platform: "network",
    delivery: "recorded",
    status: "ongoing",
    people: ["相良茉優"],
    periods: [],
    episode_count: 1,
  },
];
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
  await context.route("https://**/*", route => route.abort());
  await context.route("**/api/**", async route => {
    const pathname = new URL(route.request().url()).pathname;
    const json = data => route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    if (pathname === "/api/auth/session") return json({ authenticated: true, role: "admin" });
    if (pathname === "/api/programs") return json({ programs });
    return json({});
  });
}

(async () => {
  let browser;
  try {
    for (let i = 0; i < 100; i += 1) {
      try {
        if ((await fetch(base)).ok) break;
      } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await setup(context);
    const page = await context.newPage();

    await page.goto(`${base}/admin/programs?cast=${encodeURIComponent("大西亜玖璃")}`);
    await page.locator(".program-admin-item").first().waitFor();
    assert.equal(await page.locator(".program-admin-item").count(), 1);
    assert.equal(await page.locator(".program-admin-cast-filter .program-cast-filter-summary strong").innerText(), "已选 1 位");
    assert.equal(new URL(page.url()).searchParams.get("cast"), "大西亜玖璃");

    const filter = page.locator(".program-admin-cast-filter");
    await filter.locator("summary").click();
    await filter.getByRole("button", { name: "清空" }).click();
    await page.waitForTimeout(50);
    assert.equal(await page.locator(".program-admin-item").count(), 2);
    assert.equal(new URL(page.url()).searchParams.has("cast"), false);

    await filter.getByRole("button", { name: "相良茉優" }).click();
    await page.waitForTimeout(50);
    assert.equal(await page.locator(".program-admin-item").count(), 1);
    assert.equal(new URL(page.url()).searchParams.get("cast"), "相良茉優");
    console.log("PASS: admin programs cast query parameter is restored, updated, and cleared.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
