const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15184;
const base = `http://127.0.0.1:${port}`;
const programId = "program-78b1c903f081";
const note = "第一行备注\n第二行备注\n第三行备注";
const server = spawn(process.execPath, [
  "frontend/node_modules/vite/bin/vite.js", "frontend", "--host", "127.0.0.1", "--port", String(port), "--strictPort",
], { cwd: root, stdio: "ignore" });

async function configure(context) {
  await context.addInitScript(() => {
    localStorage.setItem("locale", "zh-CN");
    localStorage.setItem("theme", "latte");
  });
  await context.route("https://**/*", (route) => route.abort());
  await context.route("**/api/**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    let data = {};
    if (pathname === "/api/auth/session") {
      data = { authenticated: false };
    } else if (pathname === `/api/programs/${programId}`) {
      data = {
        program: {
          id: programId,
          title: "换行备注测试节目",
          category: "personal",
          format: "radio",
          platform: "network",
          delivery: "recorded",
          status: "ongoing",
          episode_count: 1,
          episode_start: 1,
          start_date: "2026-01-01",
          end_date: "",
          periods: [],
          people: [],
        },
        subprograms: [],
      };
    } else if (pathname === `/api/programs/${programId}/occurrences`) {
      data = {
        occurrences: [{
          id: "note-test-occurrence",
          episode: 1,
          original_date: "2026-01-02",
          date: "2026-01-02",
          original_time: "12:00",
          time: "12:00",
          status: "scheduled",
          aired: true,
          generated: false,
          materialized: true,
          delivery: "recorded",
          guests: [],
          absent_members: [],
          images: [],
          note,
        }],
      };
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
}

(async () => {
  let browser;
  try {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try {
        if ((await fetch(base)).ok) break;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await configure(context);
    const page = await context.newPage();
    await page.goto(`${base}/programs/archive/${programId}`);
    const noteElement = page.locator(".program-readonly-episode-note");
    await noteElement.waitFor();
    assert.equal(await noteElement.textContent(), note);
    const layout = await noteElement.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        whiteSpace: style.whiteSpace,
        height: element.getBoundingClientRect().height,
        lineHeight: Number.parseFloat(style.lineHeight),
      };
    });
    assert.equal(layout.whiteSpace, "pre-line");
    assert.ok(layout.height >= layout.lineHeight * 3 - 1, `note did not render as three lines: ${JSON.stringify(layout)}`);
    await context.close();
    console.log("PASS: program archive occurrence notes preserve stored line breaks.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
