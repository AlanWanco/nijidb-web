// Mocked browser smoke test for date picker controls. No local or remote database writes.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15174;
const base = `http://127.0.0.1:${port}`;
const server = spawn(
  process.execPath,
  ["frontend/node_modules/vite/bin/vite.js", "frontend", "--host", "127.0.0.1", "--port", String(port), "--strictPort"],
  { cwd: root, stdio: "ignore" },
);

async function configure(context) {
  await context.addInitScript(() => {
    localStorage.setItem("locale", "zh-CN");
    localStorage.setItem("theme", "latte");
  });
  await context.route("https://**/*", (route) => route.abort());
  await context.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const data = pathname === "/api/auth/session"
      ? { authenticated: true }
      : pathname === "/api/admin/collabo"
        ? { mode: "database", items: [], total: 0, page: 1, pages: 1, years: [] }
        : pathname === "/api/programs"
          ? { programs: [] }
          : {};
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
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

    const programPage = await context.newPage();
    await programPage.goto(`${base}/admin/programs`);
    await programPage.getByRole("button", { name: "新建节目" }).first().click();
    const periodCard = programPage.locator(".program-period-card").first();
    await periodCard.waitFor();
    await periodCard.getByRole("button", { name: "逐期设置" }).click();

    const dateInput = periodCard.locator(".period-start-field .program-date-picker input");
    await dateInput.click();
    const todayButton = programPage.getByRole("button", { name: "今天", exact: true });
    await todayButton.waitFor({ state: "visible" });
    await todayButton.click();
    const expectedToday = await programPage.evaluate(() => {
      const now = new Date();
      return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    });
    assert.equal(await dateInput.inputValue(), expectedToday, "Today action selects the local calendar date");

    const timeInput = periodCard.locator(".period-time-field input");
    await timeInput.click();
    const increment = programPage.locator('[data-test-id="hours-time-inc-btn-0"]');
    const decrement = programPage.locator('[data-test-id="hours-time-dec-btn-0"]');
    await increment.waitFor({ state: "visible" });
    const incrementIcon = await increment.locator("svg path").getAttribute("d");
    const decrementIcon = await decrement.locator("svg path").getAttribute("d");
    assert.match(incrementIcon, /m6 9 6 6 6-6/, "down-chevron is paired with increment");
    assert.match(decrementIcon, /m6 15 6-6 6 6/, "up-chevron is paired with decrement");

    const collaboPage = await context.newPage();
    await collaboPage.goto(`${base}/admin/collabo/new`);
    await collaboPage.locator(".cb-editor").waitFor();
    const collaboDate = collaboPage.locator(".cb-editor-fields .program-date-picker input").first();
    const collaboDateStyle = await collaboDate.evaluate((input) => {
      const picker = input.closest(".program-date-picker");
      const style = getComputedStyle(input);
      const backgroundProbe = document.createElement("span");
      backgroundProbe.style.backgroundColor = getComputedStyle(picker).getPropertyValue("--dp-background-color");
      document.body.append(backgroundProbe);
      const pickerBackground = getComputedStyle(backgroundProbe).backgroundColor;
      backgroundProbe.remove();
      return {
        background: style.backgroundColor,
        pickerBackground,
        borderRadius: style.borderRadius,
        paddingInlineStart: style.paddingInlineStart,
        minHeight: style.minHeight,
      };
    });
    assert.equal(collaboDateStyle.background, collaboDateStyle.pickerBackground);
    assert.equal(collaboDateStyle.borderRadius, "10px");
    assert.equal(collaboDateStyle.paddingInlineStart, "45px");
    assert.equal(collaboDateStyle.minHeight, "46px");

    await collaboDate.click();
    const collaboToday = collaboPage.getByRole("button", { name: "今天", exact: true });
    await collaboToday.waitFor({ state: "visible" });
    await collaboToday.click();
    assert.equal(await collaboDate.inputValue(), expectedToday);

    console.log("PASS: Today action, reversed time-chevron mapping, and collaboration date-picker styling.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
