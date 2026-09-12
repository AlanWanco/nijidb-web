// Verifies the Ctrl+K search window on the three public searchable archives.
// All APIs are mocked; this test does not touch any local or remote database.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15176;
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
    if (pathname === "/api/releases")
      return json({ releases: [], last: null });
    if (pathname === "/api/collabo")
      return json({ items: [], total: 0, pages: 1, page: 1, years: [] });
    if (pathname === "/api/news")
      return json({
        items: [],
        total: 0,
        pages: 1,
        page: 1,
        tag_options: [],
        tag_catalog: [],
        source_options: [],
      });
    return json({});
  });
}

async function openAndSearch(page, path, anchor, value, expectedPlaceholder) {
  await page.goto(base + path);
  await page.locator(anchor).waitFor();
  assert.equal(await page.locator(`${anchor} .search-shortcut-hint`).innerText(), "Ctrl+K");
  await page.keyboard.press("Control+K");

  const dialog = page.locator(".global-search-dialog");
  const input = dialog.locator("input");
  await dialog.waitFor();
  assert.equal(await input.getAttribute("placeholder"), expectedPlaceholder);
  assert.equal(await input.evaluate((element) => element === document.activeElement), true);

  const box = await dialog.boundingBox();
  assert.ok(box, "search dialog should have a bounding box");
  assert.ok(Math.abs(box.x + box.width / 2 - 720) < 2, "search dialog should be horizontally centered");
  assert.ok(box.y < 220, "search dialog should be near the top of the viewport");

  await input.fill(value);
  await input.press("Enter");
  await page.waitForURL((url) => url.searchParams.get("q") === value);
  await dialog.waitFor({ state: "detached" });
  assert.equal(await page.locator(".global-search-dialog").count(), 0);
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

    await openAndSearch(
      page,
      "/music",
      ".music-hero",
      "Test Release",
      "搜索标题或艺术家",
    );
    await openAndSearch(
      page,
      "/collabo",
      ".cb-hero",
      "Test Collaboration",
      "搜索联动、成员或合作方",
    );
    await openAndSearch(
      page,
      "/news",
      ".news-hero",
      "Test News",
      "搜索新闻标题、摘要、正文或标签",
    );

    await page.keyboard.press("Control+K");
    await page.locator(".global-search-dialog").waitFor();
    await page.keyboard.press("Escape");
    await page.locator(".global-search-dialog").waitFor({ state: "detached" });
    assert.equal(await page.locator(".global-search-dialog").count(), 0);
    console.log("PASS: Ctrl+K search window, focus, positioning, submission, and Escape close.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
