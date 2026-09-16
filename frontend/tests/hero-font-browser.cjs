// Verifies the language-specific font stacks used by the archive page titles.
// All APIs are mocked; this test does not touch any local or remote database.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15180;
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
    if (!localStorage.getItem("locale")) localStorage.setItem("locale", "zh-CN");
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
    if (pathname === "/api/releases") return json({ releases: [], last: null });
    if (pathname === "/api/programs/calendar") return json({ programs: [], events: [] });
    if (pathname === "/api/auth/session") return json({ authenticated: false });
    if (pathname === "/api/collaboration-illustrations") return json({ items: {} });
    if (pathname.startsWith("/api/collabo")) {
      return json({ items: [], total: 0, pages: 1, page: 1, years: [] });
    }
    if (pathname === "/api/news") {
      return json({
        items: [],
        total: 0,
        pages: 1,
        page: 1,
        tag_options: [],
        tag_catalog: [],
        source_options: [],
      });
    }
    return json({});
  });
}

async function assertHeroFont(page, path, selector, expectedFamily) {
  await page.goto(base + path);
  const title = page.locator(selector);
  await title.waitFor();
  const details = await title.evaluate(async (element) => {
    await Promise.all([
      document.fonts.load('800 60px "Logo SC Unbounded Sans"', element.textContent),
      document.fonts.load('800 60px "Dela Gothic One"', element.textContent),
    ]);
    return {
      family: getComputedStyle(element).fontFamily,
      lang: document.documentElement.lang,
      logoLoaded: document.fonts.check('800 60px "Logo SC Unbounded Sans"'),
      delaLoaded: document.fonts.check('800 60px "Dela Gothic One"'),
    };
  });
  assert.match(details.family, new RegExp(expectedFamily));
  assert.equal(details.logoLoaded, true);
  assert.equal(details.delaLoaded, true);
  return details;
}

async function assertTitleGlyphCoverage(page, cdp, selector, expectedText, expectedFamily) {
  const title = page.locator(selector);
  assert.equal((await title.textContent()).trim(), expectedText);
  const documentNode = await cdp.send("DOM.getDocument");
  const node = await cdp.send("DOM.querySelector", {
    nodeId: documentNode.root.nodeId,
    selector,
  });
  assert.notEqual(node.nodeId, 0);
  const platformFonts = (await cdp.send("CSS.getPlatformFontsForNode", { nodeId: node.nodeId })).fonts;
  const normalizeFamily = (value) => String(value || "").replace(/\s/g, "");
  const customFont = platformFonts.find(
    (font) => font.isCustomFont && normalizeFamily(font.familyName) === normalizeFamily(expectedFamily),
  );
  assert.ok(customFont, `${expectedText} did not use ${expectedFamily}: ${JSON.stringify(platformFonts)}`);
  assert.equal(customFont.glyphCount, [...expectedText].length);
  assert.equal(
    platformFonts.filter((font) => !font.isCustomFont && font.glyphCount > 0).length,
    0,
    `${expectedText} still uses a fallback font: ${JSON.stringify(platformFonts)}`,
  );
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
    const cdp = await context.newCDPSession(page);
    await cdp.send("DOM.enable");
    await cdp.send("CSS.enable");
    const fontRequests = [];
    page.on("request", request => {
      if (request.url().includes("/assets/fonts/")) fontRequests.push(request.url());
    });
    const heroes = [
      ["/music", ".music-hero h1"],
      ["/collabo", ".cb-hero h1"],
      ["/news", ".news-hero h1"],
      ["/programs", ".programs-topline h1"],
    ];

    for (const [path, selector] of heroes) {
      const details = await assertHeroFont(page, path, selector, "Logo SC Unbounded Sans");
      assert.equal(details.lang, "zh-CN");
    }
    await assertHeroFont(page, "/programs/archive", ".programs-topline h1", "Logo SC Unbounded Sans");
    await assertTitleGlyphCoverage(page, cdp, ".programs-topline h1", "节目列表", "Logo SC Unbounded Sans");

    await page.evaluate(() => localStorage.setItem("locale", "ja"));
    for (const [path, selector] of heroes) {
      const details = await assertHeroFont(page, path, selector, "Dela Gothic One");
      assert.equal(details.lang, "ja-JP");
    }
    await assertHeroFont(page, "/programs/archive", ".programs-topline h1", "Dela Gothic One");
    await assertTitleGlyphCoverage(page, cdp, ".programs-topline h1", "番組一覧", "Dela Gothic One");
    assert.equal(fontRequests.some(url => url.endsWith("LogoSCUnboundedSans-hero.woff2")), true);
    assert.equal(fontRequests.some(url => url.endsWith("DelaGothicOne-hero.woff2")), true);
    assert.equal(fontRequests.some(url => /\.(?:otf|ttf)(?:$|\?)/i.test(url)), false);
    console.log("PASS: Chinese and Japanese archive-title font stacks, subset WOFF2 loading, and programs title.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
