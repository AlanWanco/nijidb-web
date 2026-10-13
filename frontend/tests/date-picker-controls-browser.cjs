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
          : pathname === "/api/releases"
            ? { releases: [{ id: "touch-test", title: "Touch test", artist: "Artist", cover_url: "" }], last: null }
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
    await programPage.locator("main#main-content").waitFor();
    await programPage.keyboard.press("Tab");
    assert.equal(await programPage.evaluate(() => document.activeElement.classList.contains("skip-link")), true);
    await programPage.keyboard.press("Enter");
    await programPage.waitForFunction(() => document.activeElement?.id === "main-content");

    await programPage.getByRole("button", { name: "新建节目" }).first().click();
    const periodCard = programPage.locator(".program-period-card").first();
    await periodCard.waitFor();
    const fixedPeopleGroup = programPage.getByRole("group", { name: "固定参与成员 / 主持" });
    const firstMember = fixedPeopleGroup.getByRole("button").first();
    const initialMemberState = await firstMember.getAttribute("aria-pressed");
    await firstMember.click();
    assert.notEqual(await firstMember.getAttribute("aria-pressed"), initialMemberState);
    await firstMember.click();
    assert.equal(await firstMember.getAttribute("aria-pressed"), initialMemberState);

    const scheduleModes = periodCard.getByRole("group", { name: "更新方式" });
    assert.equal(await scheduleModes.getByRole("button", { name: "周更" }).getAttribute("aria-pressed"), "true");
    await scheduleModes.getByRole("button", { name: "逐期设置" }).click();
    assert.equal(await scheduleModes.getByRole("button", { name: "逐期设置" }).getAttribute("aria-pressed"), "true");
    assert.equal(await scheduleModes.getByRole("button", { name: "周更" }).getAttribute("aria-pressed"), "false");

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

    const touchContext = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    });
    await configure(touchContext);
    const touchPage = await touchContext.newPage();
    await touchPage.goto(`${base}/admin/programs`);
    await touchPage.getByRole("button", { name: "新建节目" }).first().click();
    const touchPeriod = touchPage.locator(".program-period-card").first();
    await touchPeriod.getByRole("button", { name: "逐期设置" }).click();
    const touchDate = touchPeriod.locator(".period-start-field .program-date-picker input");
    await touchDate.click();
    const touchCalendarSizes = await touchPage.locator(".dp__menu").evaluate((menu) => {
      const size = (element) => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right, width: rect.width, height: rect.height };
      };
      return {
        coarsePointer: matchMedia("(pointer: coarse)").matches,
        menu: size(menu),
        navigation: size(menu.querySelector(".dp--arrow-btn-nav")),
        day: size(menu.querySelector(".dp__cell_inner:not(.dp__cell_disabled)")),
        today: size(menu.querySelector(".dp__action_button")),
      };
    });
    assert.equal(touchCalendarSizes.coarsePointer, true);
    for (const control of [touchCalendarSizes.navigation, touchCalendarSizes.day, touchCalendarSizes.today]) {
      assert.ok(control.width >= 43 && control.height >= 43, `touch calendar target is too small: ${JSON.stringify(control)}`);
    }
    assert.ok(touchCalendarSizes.menu.left >= 0 && touchCalendarSizes.menu.right <= 390, `touch calendar overflows viewport: ${JSON.stringify(touchCalendarSizes.menu)}`);

    await touchPage.keyboard.press("Escape");
    const touchTime = touchPeriod.locator(".period-time-field input");
    await touchTime.click();
    const touchIncrement = touchPage.locator('[data-test-id="hours-time-inc-btn-0"]');
    await touchIncrement.waitFor({ state: "visible" });
    const touchTimeSize = await touchIncrement.evaluate((button) => {
      const rect = button.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    });
    assert.ok(touchTimeSize.width >= 43 && touchTimeSize.height >= 43, `touch time target is too small: ${JSON.stringify(touchTimeSize)}`);

    await touchPage.keyboard.press("Escape");
    await touchPage.setViewportSize({ width: 320, height: 720 });
    await touchDate.click();
    const compactCalendar = await touchPage.locator(".dp__menu").evaluate((menu) => {
      const menuRect = menu.getBoundingClientRect();
      const dayRect = menu.querySelector(".dp__cell_inner:not(.dp__cell_disabled)").getBoundingClientRect();
      return { left: menuRect.left, right: menuRect.right, dayWidth: dayRect.width, dayHeight: dayRect.height };
    });
    assert.ok(compactCalendar.left >= 0 && compactCalendar.right <= 320, `compact calendar overflows viewport: ${JSON.stringify(compactCalendar)}`);
    assert.ok(compactCalendar.dayWidth >= 39 && compactCalendar.dayHeight >= 39, `compact calendar targets are too small: ${JSON.stringify(compactCalendar)}`);

    const touchMusicPage = await touchContext.newPage();
    await touchMusicPage.goto(`${base}/music`);
    const touchCover = touchMusicPage.locator(".cover-tile").first();
    await touchCover.waitFor();
    const coverAffordance = await touchCover.locator(".cover-hover").evaluate((element) => {
      const style = getComputedStyle(element);
      return { display: style.display, opacity: style.opacity, transform: style.transform };
    });
    assert.notEqual(coverAffordance.display, "none");
    assert.equal(coverAffordance.opacity, "1");
    assert.equal(coverAffordance.transform, "none");

    console.log("PASS: skip link, selected button semantics, touch targets, touch affordance, Today action, reversed time arrows, and collaboration date styling.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
