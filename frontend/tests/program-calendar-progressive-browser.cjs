// Public calendar caching/reveal tests. Every API is mocked; no production or local DB is touched.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15182;
const base = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ["frontend/node_modules/vite/bin/vite.js", "frontend", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: root, stdio: "ignore" });
const gate = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

function snapshot(month, revision = 1) {
  const prefix = `2026-${String(month).padStart(2, "0")}`;
  const program = { id: "one", title: `本地日历 v${revision}`, category: "personal", format: "video", platform: "network", delivery: "recorded", people: ["大西亜玖璃"], status: "ongoing", start_date: "2025-01-01", periods: [], episode_count: 40 };
  const days = revision === 2 ? [1, 3, 8, 15, 22, 27] : [1, 2, 3, 8, 9, 15, 16, 22, 23, 27, 28];
  const events = days.flatMap(day => Array.from({ length: 4 }, (_, index) => {
    const date = `${prefix}-${String(day).padStart(2, "0")}`;
    return {
      id: `${prefix}-${day}-${index}`,
      title: `${program.title} · 第 ${day} 期 · ${index} · 用于测试很长的节目标题和日历滚动时的渐进入场`,
      start: date, allDay: true,
      extendedProps: { programId: "one", programTitle: program.title, category: "personal", format: "video", delivery: "recorded", status: "ongoing", episode: day, originalDate: date, originalTime: "", originalStart: date, timezone: "Asia/Tokyo", occurrenceStatus: "scheduled", aired: true, updateStatus: "updated", people: ["大西亜玖璃"], guests: [], absentMembers: [], images: [], note: "测试" },
    };
  }));
  return { programs: [program], events, start: `${prefix}-01`, end: `${prefix}-28` };
}

async function setup(context, state) {
  await context.addInitScript(() => { localStorage.setItem("locale", "zh-CN"); localStorage.setItem("theme", "latte"); });
  await context.route("https://**/*", route => route.abort());
  await context.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/auth/session") return route.fulfill({ contentType: "application/json", body: JSON.stringify({ authenticated: false }) });
    if (url.pathname !== "/api/programs/calendar") return route.fulfill({ contentType: "application/json", body: "{}" });
    const start = url.searchParams.get("start");
    const mid = new Date(new Date(`${start}T12:00:00Z`).getTime() + 18 * 86400000);
    const month = mid.getUTCMonth() + 1;
    const revision = state.revisions?.[month] || 1;
    const tag = `W/"calendar-${month}-${revision}"`;
    state.requests.push({ month, tag: request.headers()["if-none-match"] });
    const pending = state.gates?.[month];
    if (pending) await pending.promise;
    if (state.failure) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "测试网络暂不可用" }) });
    if (request.headers()["if-none-match"] === tag) return route.fulfill({ status: 304, headers: { ETag: tag } });
    const data = snapshot(month, revision);
    data.start = start;
    data.end = new Date(new Date(`${url.searchParams.get("end")}T00:00:00Z`).getTime() - 86400000).toISOString().slice(0, 10);
    return route.fulfill({ contentType: "application/json", headers: { ETag: tag, "Cache-Control": "private, no-cache" }, body: JSON.stringify(data) });
  });
}

async function waitForStoredSnapshot(page) {
  await page.waitForFunction(() => new Promise(resolve => {
    const request = indexedDB.open("nijidb-program-calendar-v1", 1);
    request.onerror = () => resolve(false);
    request.onsuccess = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("windows")) { db.close(); resolve(false); return; }
      const query = db.transaction("windows").objectStore("windows").count();
      query.onsuccess = () => { db.close(); resolve(query.result > 0); };
    };
  }));
}

(async () => {
  let browser;
  try {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try { if ((await fetch(base)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const first = gate();
    const state = { requests: [], gates: { 10: first }, revisions: {} };
    await setup(context, state);
    const page = await context.newPage();
    const pageErrors = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    await page.goto(`${base}/programs/202610`, { waitUntil: "domcontentloaded" });
    await page.getByText("正在加载日历……", { exact: true }).waitFor();
    assert.ok(await page.locator(".fc-daygrid-day").count() >= 28);
    assert.equal(await page.locator(".fc-daygrid-event").count(), 0);
    assert.equal(await page.locator(".program-calendar-initial-loading").count(), 1);
    first.resolve();
    await page.waitForFunction(() => document.querySelectorAll(".fc-daygrid-event").length === 44, null, { timeout: 8000 }).catch(async error => {
      console.error("calendar-debug", JSON.stringify({ requests: state.requests, pageErrors, errorText: await page.locator(".state.error").allTextContents(), eventCount: await page.locator(".fc-daygrid-event").count() }));
      throw error;
    });
    await page.waitForTimeout(350);
    assert.ok(await page.locator(".program-event-reveal-pending").count() > 0);
    const pending = page.locator(".program-event-reveal-pending").last();
    assert.equal(await pending.evaluate(element => getComputedStyle(element).opacity), "0");
    await page.evaluate(() => document.body.classList.add("program-screenshot-capturing"));
    assert.equal(await pending.evaluate(element => getComputedStyle(element).opacity), "1");
    await page.evaluate(() => document.body.classList.remove("program-screenshot-capturing"));
    const last = page.locator(".fc-daygrid-event").last();
    await last.scrollIntoViewIfNeeded();
    await page.waitForFunction(() => ![...document.querySelectorAll(".fc-daygrid-event")].at(-1)?.classList.contains("program-event-reveal-pending"));
    await page.waitForTimeout(350);
    assert.equal(await last.evaluate(element => getComputedStyle(element).opacity), "1");
    await waitForStoredSnapshot(page);

    // A fresh page has no in-memory cache; it must use IndexedDB before the held request resolves.
    const warm = gate();
    state.gates[10] = warm;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByText("已显示缓存，正在检查更新……", { exact: true }).waitFor();
    assert.equal(await page.locator(".fc-daygrid-event").count(), 44);
    assert.equal(state.requests.at(-1).tag, 'W/"calendar-10-1"');
    const countBefore = state.requests.length;
    warm.resolve();
    await page.getByText("已显示缓存，正在检查更新……", { exact: true }).waitFor({ state: "hidden" });
    assert.equal(state.requests.length, countBefore);
    assert.equal(await page.locator(".fc-daygrid-event").count(), 44);

    const changed = gate();
    state.gates[10] = changed;
    state.revisions[10] = 2;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByText("已显示缓存，正在检查更新……", { exact: true }).waitFor();
    assert.equal(await page.locator(".fc-daygrid-event").count(), 44);
    changed.resolve();
    await page.waitForFunction(() => document.querySelectorAll(".fc-daygrid-event").length === 24);
    assert.match(await page.locator(".fc-daygrid-event").first().innerText(), /v2/);
    await page.waitForFunction(() => new Promise(resolve => {
      const request = indexedDB.open("nijidb-program-calendar-v1", 1);
      request.onsuccess = () => {
        const db = request.result;
        const query = db.transaction("windows").objectStore("windows").getAll();
        query.onsuccess = () => { db.close(); resolve(query.result.some(row => row.etag === 'W/"calendar-10-2"')); };
      };
    }));
    state.failure = true;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByText("更新失败，暂时显示缓存", { exact: true }).waitFor();
    assert.equal(await page.locator(".fc-daygrid-event").count(), 24);
    state.failure = false;

    // Fast month changes must not let a delayed November response replace December.
    state.gates[11] = gate();
    state.gates[12] = gate();
    await page.getByRole("button", { name: "下个月", exact: true }).first().click();
    for (let attempt = 0; attempt < 30 && !state.requests.some(item => item.month === 11); attempt += 1) await page.waitForTimeout(50);
    await page.getByRole("button", { name: "下个月", exact: true }).first().click();
    for (let attempt = 0; attempt < 30 && !state.requests.some(item => item.month === 12); attempt += 1) await page.waitForTimeout(50);
    state.gates[12].resolve();
    await page.waitForFunction(() => [...document.querySelectorAll(".fc-daygrid-event")].some(element => element.textContent.includes("v1")));
    state.gates[11].resolve();
    await page.waitForTimeout(200);
    assert.match(await page.locator(".program-calendar-sticky-month > strong").innerText(), /2026.*12/);
    assert.equal(await page.locator('[data-date="2026-12-01"] .fc-daygrid-event').count(), 4);
    assert.equal(pageErrors.length, 0, pageErrors.join("\n"));
    await context.close();

    const reduced = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: "reduce" });
    const reducedState = { requests: [], gates: {} };
    await setup(reduced, reducedState);
    const mobile = await reduced.newPage();
    await mobile.goto(`${base}/programs/202610`);
    await mobile.waitForFunction(() => document.querySelectorAll(".fc-daygrid-event").length === 44);
    assert.equal(await mobile.locator(".program-event-reveal-pending").count(), 0);
    assert.equal(await mobile.locator(".fc-daygrid-event").last().evaluate(element => getComputedStyle(element).opacity), "1");
    await reduced.close();

    const blocked = await browser.newContext();
    await blocked.addInitScript(() => Object.defineProperty(window, "indexedDB", { get() { throw new DOMException("blocked", "SecurityError"); } }));
    const blockedState = { requests: [], gates: {} };
    await setup(blocked, blockedState);
    const fallback = await blocked.newPage();
    await fallback.goto(`${base}/programs/202610`);
    await fallback.waitForFunction(() => document.querySelectorAll(".fc-daygrid-event").length === 44);
    assert.equal(await fallback.locator(".state.error").count(), 0);
    await blocked.close();
    console.log("PASS: skeleton grid, viewport reveal, screenshot completeness, IndexedDB cache-first reload, 304, changed/deleted events, offline cached display, month-race cancellation, reduced motion, and storage-disabled fallback.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
