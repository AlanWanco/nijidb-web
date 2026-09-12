// Read-only fixtures; every backend request is mocked. No production or local database writes.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("fs");
const path = require("path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "../..");
const catalog = JSON.parse(fs.readFileSync(path.join(root, "frontend/src/content/collaborationIllustrations.json")));
const manifest = JSON.parse(fs.readFileSync(path.join(root, "data/images/illustrations/manifest.json")));
const rawItems = catalog.years
  .flatMap((y) => y.items)
  .sort((a, b) => b.first_seen.localeCompare(a.first_seen) || a.id.localeCompare(b.id));
const images = (id) =>
  (manifest.items[id]?.images || []).map((i) => ({
    ...i,
    path: i.path.replace("/media/illustrations/", "/api/collaboration-illustrations/assets/"),
  }));
const sample = rawItems.find((i) => images(i.id).length > 1);
const slug = (i) => `${i.first_seen.replaceAll("-", "")}-${i.id.slice(0, 6)}`;
let dbMode = false;
let saves = 0;
let collaboDeleted = false;
const dbItem = {
  ...sample,
  slug: slug(sample),
  images: images(sample.id),
  tags: [
    "ayumu",
    "kasumi",
    "shizuku",
    "karin",
    "ai",
    "kanata",
    "setsuna",
    "emma",
    "rina",
  ],
  periods: [
    {
      start_date: "2026-01-01",
      end_date: "2026-01-31",
      title: "第一期",
      description: "活动说明",
    },
  ],
  review_status: "pending",
};
const browserErrors = [];
async function configure(context) {
  await context.addInitScript(() => {
    localStorage.setItem("locale", "zh-CN");
    localStorage.setItem("theme", "latte");
  });
  await context.route("**/api/**", async (route) => {
    const u = new URL(route.request().url());
    const p = u.pathname;
    const json = (data) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    if (p.startsWith("/api/collaboration-illustrations/assets/")) {
      const asset = p.replace("/api/collaboration-illustrations/assets/", "");
      const target = path.join(root, "data/images/illustrations", asset);
      if (fs.existsSync(target))
        return route.fulfill({
          status: 200,
          contentType: asset.endsWith("png") ? "image/png" : "image/jpeg",
          body: fs.readFileSync(target),
        });
    }
    if (p === "/api/collaboration-illustrations")
      return json({
        available: true,
        items: Object.fromEntries(rawItems.map((i) => [i.id, { images: images(i.id) }])),
      });
    if (p === "/api/auth/session") return json({ authenticated: true });
    if (p.startsWith("/api/releases/")) {
      const id = p.split("/").pop();
      return json({
        release: { id, title: `CD ${id}`, source_url: "https://example.com", cover_url: images(sample.id)[0].path },
        previous: id === "two" ? { id: "one", title: "CD one" } : null,
        following: id === "one" ? { id: "two", title: "CD two" } : null,
      });
    }
    if (dbMode && (p === "/api/admin/collabo" || p === "/api/collabo")) {
      const isAdminList = p === "/api/admin/collabo";
      const requestedPage = Number(u.searchParams.get("page") || 1);
      return json({
        items: collaboDeleted ? [] : [dbItem],
        total: collaboDeleted ? 0 : isAdminList ? 49 : 1,
        page: collaboDeleted ? 1 : Math.min(Math.max(1, requestedPage), isAdminList ? 3 : 1),
        pages: collaboDeleted ? 1 : isAdminList ? 3 : 1,
        years: ["2026"],
        source: catalog.source,
      });
    }
    if (dbMode && (p === `/api/admin/collabo/${sample.id}` || p === `/api/collabo/${slug(sample)}`)) {
      if (route.request().method() === "DELETE") {
        collaboDeleted = true;
        return json({ message: "联动已删除" });
      }
      if (route.request().method() === "PATCH") {
        saves++;
        Object.assign(dbItem, route.request().postDataJSON());
      }
      return json({ item: dbItem, source: catalog.source, previous: null, following: { id: "next", title: "Next item" } });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"Not Found"}' });
  });
}
async function swipe(page, selector, dx, dy = 2) {
  await page.locator(selector).evaluate(
    (el, { dx, dy }) => {
      const r = el.getBoundingClientRect();
      const x = dx < 0 ? Math.min(innerWidth - 60, 290) : 65;
      const y = Math.max(180, Math.min(r.top + 50, innerHeight - 80));
      const point = (px, py) => new Touch({ identifier: 1, target: el, clientX: px, clientY: py });
      el.dispatchEvent(
        new TouchEvent("touchstart", { bubbles: true, touches: [point(x, y)], changedTouches: [point(x, y)] }),
      );
      el.dispatchEvent(
        new TouchEvent("touchmove", {
          bubbles: true,
          touches: [point(x + dx, y + dy)],
          changedTouches: [point(x + dx, y + dy)],
        }),
      );
      el.dispatchEvent(
        new TouchEvent("touchend", { bubbles: true, touches: [], changedTouches: [point(x + dx, y + dy)] }),
      );
    },
    { dx, dy },
  );
}
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  try {
    const desktop = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await configure(desktop);
    const page = await desktop.newPage();
    page.on("pageerror", (e) => browserErrors.push(e.message));
    await page.goto("http://127.0.0.1:15173/collabo");
    await page.waitForSelector(".cb-card");
    assert.equal(await page.title(), "联动立绘 · Nijigasaki DB");
    const filterAlignment = await page.evaluate(() => {
      const year = document.querySelector(".cb-year-filter").getBoundingClientRect();
      const characters = document.querySelector(".cb-character-filter").getBoundingClientRect();
      const chip = document.querySelector(".cb-character-chip").getBoundingClientRect();
      return {
        left: Math.abs(year.left - characters.left),
        width: Math.abs(year.width - characters.width),
        topGap: chip.top - characters.top,
        bottomGap: characters.bottom - chip.bottom,
      };
    });
    assert.ok(filterAlignment.left < 1, "character filter left edge is centered with year filter");
    assert.ok(filterAlignment.width < 1, "character filter width matches year filter");
    assert.ok(filterAlignment.topGap >= 8, "character chips are separated from the upper divider");
    assert.ok(filterAlignment.bottomGap >= 8, "character chips are separated from the lower divider");
    assert.equal(
      await page
        .getByRole("button", { name: "高咲侑", exact: true })
        .locator(".cb-character-dot")
        .evaluate((element) => getComputedStyle(element).backgroundColor),
      "rgb(47, 47, 47)",
    );
    assert.equal(await page.locator(".cb-hero .cb-search").count(), 1);
    assert.equal(await page.locator(".cb-toolbar .cb-search").count(), 0);
    assert.equal(await page.locator(".cb-card").count(), 24);
    assert.equal((await page.locator(".cb-card img").count()) <= 24, true);
    const heroBounds = await page.locator(".cb-hero").boundingBox();
    await page.mouse.move(heroBounds.x + heroBounds.width * 0.8, heroBounds.y + heroBounds.height * 0.35);
    await page.waitForTimeout(100);
    assert.notEqual(
      await page
        .locator(".cb-hero")
        .evaluate((element) => getComputedStyle(element).getPropertyValue("--cb-hero-focus-x")),
      "0px",
    );
    await page.evaluate(() => window.scrollTo(0, 900));
    const filterScroll = await page.evaluate(() => window.scrollY);
    await page.getByRole("button", { name: "2026", exact: true }).evaluate((element) => element.click());
    await page.waitForURL(/year=2026/);
    await page.waitForTimeout(300);
    assert.ok(
      Math.abs((await page.evaluate(() => window.scrollY)) - filterScroll) < 100,
      "preserve year filter scroll",
    );
    assert.equal(await page.locator(".cb-year-chip.selected").count(), 1);
    await page.getByRole("button", { name: "2025", exact: true }).evaluate((element) => element.click());
    await page.waitForURL(/year=2026%2C2025|year=2025%2C2026|year=2026,2025|year=2025,2026/);
    assert.equal(await page.locator(".cb-year-chip.selected").count(), 2);
    await page.getByRole("button", { name: "2026", exact: true }).click({ button: "right" });
    await page.waitForURL((url) => url.searchParams.get("year") === "2026");
    assert.equal(await page.locator(".cb-year-chip.selected").count(), 1);
    await page.getByRole("button", { name: /全部年份/ }).click();
    await page.waitForURL((url) => !url.searchParams.has("year"));
    assert.equal(await page.locator(".cb-year-chip.selected").count(), 1);
    const combinationFilter = page.locator(".cb-combination-filter");
    assert.equal(await combinationFilter.count(), 1);
    assert.equal(await combinationFilter.getByRole("button", { name: "全员", exact: true }).count(), 1);
    for (const label of [
      "偶像12人",
      "一年级",
      "二年级",
      "三年级",
      "剧场版第一章组",
      "剧场版第二章组",
      "初始9人",
      "动画一期10人",
      "栞子加入后10人",
    ]) {
      assert.equal(await page.getByRole("button", { name: label, exact: true }).count(), 1);
    }
    for (const label of ["AZUNA", "DiverDiva", "R3BIRTH", "QU4RTZ"]) {
      assert.equal(await page.getByRole("button", { name: label, exact: true }).count(), 0);
    }
    await combinationFilter.getByRole("button", { name: "全员", exact: true }).click();
    await page.waitForURL(/group=all/);
    assert.equal(await combinationFilter.locator(".cb-combination-chip.selected").count(), 1);
    await combinationFilter.getByRole("button", { name: "全员", exact: true }).click();
    await page.waitForURL((url) => !url.searchParams.has("group"));
    await page.getByRole("button", { name: "初始9人", exact: true }).click();
    await page.waitForURL(/group=initial9/);
    assert.equal(await page.locator(".cb-combination-chip.selected").count(), 1);
    await page.getByRole("button", { name: "初始9人", exact: true }).click();
    await page.waitForURL((url) => !url.searchParams.has("group"));
    await page.getByRole("button", { name: "上原步梦", exact: true }).click({ button: "right" });
    await page.waitForURL(/tags=ayumu/);
    assert.equal(await page.locator(".cb-character-filter .cb-character-chip.selected").count(), 1);
    await page.getByRole("button", { name: "中须霞", exact: true }).click();
    await page.waitForURL(/tags=ayumu%2Ckasumi|tags=ayumu,kasumi/);
    assert.equal(await page.locator(".cb-character-filter .cb-character-chip.selected").count(), 2);
    await page.getByRole("button", { name: "上原步梦", exact: true }).click({ button: "right" });
    await page.waitForURL(/tags=ayumu/);
    assert.equal(await page.locator(".cb-character-filter .cb-character-chip.selected").count(), 1);
    await page.locator(".cb-character-filter").getByRole("button", { name: /全员/ }).click();
    await page.waitForURL((url) => !url.searchParams.has("tags"));
    assert.equal(await page.locator(".cb-character-filter .cb-character-chip.selected").count(), 1);
    await page.screenshot({ path: "/tmp/nijidb-collabo-desktop.png", fullPage: false });
    await page.getByRole("button", { name: "夜间", exact: true }).click();
    await page.screenshot({ path: "/tmp/nijidb-collabo-dark.png", fullPage: false });
    await page.evaluate(() => window.scrollTo(0, 1650));
    const listScroll = await page.evaluate(() => window.scrollY);
    await page
      .locator(".cb-card")
      .nth(14)
      .evaluate((element) => element.click());
    await page.waitForSelector(".cb-detail-layout");
    await page.getByRole("link", { name: "返回联动一览", exact: false }).click();
    await page.waitForSelector(".cb-card");
    await page.waitForTimeout(700);
    assert.ok(Math.abs((await page.evaluate(() => window.scrollY)) - listScroll) < 100, "restore list scroll");
    await page.goto(`http://127.0.0.1:15173/collabo/${slug(sample)}`);
    await page.waitForSelector(".cb-detail-layout");
    assert.equal(await page.title(), `${sample.title} · Nijigasaki DB`);
    await page.waitForFunction(() => document.querySelector(".cb-gallery-stage img")?.naturalWidth > 0);
    assert.equal((await page.locator(".cb-related a").count()) >= sample.official_links.length, true);
    await page.screenshot({ path: "/tmp/nijidb-collabo-detail.png", fullPage: false });
    await page.getByRole("button", { name: "查看大图", exact: true }).click();
    assert.equal(await page.locator("dialog[open]").count(), 1);
    const url = page.url();
    await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(200);
    assert.equal(page.url(), url);
    assert.match(await page.locator(".cb-lightbox-bar").innerText(), /^2 \/ /);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(100);
    assert.equal(await page.locator("dialog[open]").count(), 0);
    assert.equal(await page.evaluate(() => document.body.style.overflow), "");
    await page.goto(`http://127.0.0.1:15173/admin/collabo/${sample.id}`);
    await page.waitForSelector(".cb-editor");
    assert.equal(await page.locator(".cb-editor-assets .cb-upload-actions").count(), 1);
    assert.equal(
      await page.getByText("全部审核完成后，再批量生成 R2 缩略图。此页面不会提前生成或覆盖原图。", { exact: true }).count(),
      0,
    );
    assert.equal(
      await page.locator(".cb-editor-assets .cb-image-editor").first().evaluate((image) => {
        const actions = image.closest(".cb-edit-section").querySelector(".cb-upload-actions");
        return Boolean(image.compareDocumentPosition(actions) & Node.DOCUMENT_POSITION_FOLLOWING);
      }),
      true,
    );
    assert.equal(await page.getByRole("button", { name: "保存到数据库", exact: true }).isDisabled(), true);
    await page.getByLabel("标题", { exact: true }).fill("edited local draft");
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "导出草稿 JSON" }).click();
    const download = await downloadPromise;
    const payload = JSON.parse(fs.readFileSync(await download.path(), "utf8"));
    assert.equal(payload.item.title, "edited local draft");
    let leaveDialogs = 0;
    page.on("dialog", (d) => {
      leaveDialogs++;
      d.accept();
    });
    dbMode = true;
    await page.reload();
    await page.waitForSelector(".cb-editor");
    leaveDialogs = 0;
    assert.equal(await page.getByRole("button", { name: "保存到数据库", exact: true }).isEnabled(), true);
    const editorActionPaddings = await page.locator(".cb-editor-action-button").evaluateAll((buttons) =>
      buttons.map((button) => getComputedStyle(button).padding),
    );
    assert.deepEqual([...new Set(editorActionPaddings)], ["8px 12px"]);
    await page.getByLabel("标题", { exact: true }).fill("Database save test");
    assert.equal(await page.getByRole("button", { name: "上原步梦", exact: true }).count(), 1);
    assert.equal(await page.getByText("时间段 1", { exact: true }).count(), 1);
    await page.getByRole("radio", { name: "已审核", exact: true }).click();
    assert.equal(await page.locator(".cb-image-controls select").count(), 0);
    assert.equal(
      await page.locator(".cb-image-controls .cb-image-remove").first().evaluate((button) => getComputedStyle(button).color),
      "rgb(231, 130, 132)",
    );
    await page.getByRole("button", { name: "保存到数据库", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "保存成功" }).waitFor();
    assert.equal(saves, 1);
    await page.getByRole("link", { name: "管理联动", exact: false }).first().click();
    await page.waitForSelector(".cb-review-list");
    assert.equal(leaveDialogs, 0);
    const adminSearchStyle = await page.locator(".cb-admin-search-toolbar").evaluate((form) => {
      const input = form.querySelector("input");
      const button = form.querySelector("button");
      return {
        inputPadding: getComputedStyle(input).padding,
        inputWidth: input.getBoundingClientRect().width,
        buttonWidth: button.getBoundingClientRect().width,
      };
    });
    assert.equal(adminSearchStyle.inputPadding, "7px 10px");
    assert.ok(adminSearchStyle.inputWidth > adminSearchStyle.buttonWidth * 3, "admin search input is longer than button");
    await page.locator(".cb-pagination button").last().click();
    await page.waitForURL((url) => url.pathname === "/admin/collabo" && url.searchParams.get("page") === "2");
    assert.equal(new URL(page.url()).searchParams.get("page"), "2");
    await page.goto("http://127.0.0.1:15173/collabo");
    await page.waitForSelector(".cb-card");
    assert.equal(await page.locator(".cb-card-tags").innerText(), "初始9人");
    await page.goto(`http://127.0.0.1:15173/admin/collabo/${sample.id}`);
    await page.waitForSelector(".cb-editor");
    assert.equal(await page.getByRole("button", { name: "删除联动", exact: true }).count(), 1);
    await page.getByRole("button", { name: "删除联动", exact: true }).click();
    assert.equal(leaveDialogs, 2);
    await page.waitForURL("**/admin/collabo");
    await page.waitForSelector(".cb-review-list");
    assert.equal(await page.locator(".cb-review-list > a").count(), 0);
    const deleted = rawItems.find((item) => item.id !== sample.id);

    await page.goto(`http://127.0.0.1:15173/collabo/${slug(deleted)}`);
    await page.getByRole("alert").filter({ hasText: "未找到联动" }).waitFor();
    assert.equal(await page.locator(".cb-detail-layout").count(), 0);
    dbMode = false;
    const mobile = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
      deviceScaleFactor: 1,
    });
    await configure(mobile);
    const phone = await mobile.newPage();
    phone.on("pageerror", (e) => browserErrors.push(e.message));
    await phone.goto("http://127.0.0.1:15173/collabo");
    await phone.waitForSelector(".cb-card");
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await phone.screenshot({ path: "/tmp/nijidb-collabo-mobile.png", fullPage: false });
    await phone.goto(`http://127.0.0.1:15173/collabo/${slug(sample)}`);
    await phone.waitForSelector(".cb-gallery-stage");
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await phone.screenshot({ path: "/tmp/nijidb-collabo-mobile-detail.png", fullPage: false });
    const before = phone.url();
    await swipe(phone, ".cb-gallery-stage", -110, 80);
    assert.equal(phone.url(), before);
    await swipe(phone, ".cb-gallery-stage", -110);
    await phone.waitForTimeout(600);
    assert.notEqual(phone.url(), before);
    await phone.goto("http://127.0.0.1:15173/release/one");
    await phone.waitForSelector(".release");
    await swipe(phone, ".release-cover-art", -110);
    await phone.waitForURL("**/release/two");
    await phone.waitForSelector(".release");
    await swipe(phone, ".release-cover-art", 110);
    await phone.waitForURL("**/release/one");
    for (const width of [320, 768, 1024]) {
      await phone.setViewportSize({ width, height: 1000 });
      await phone.goto("http://127.0.0.1:15173/collabo");
      await phone.waitForSelector(".cb-card");
      assert.equal(
        await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
        `list width ${width}`,
      );
      await phone.goto(`http://127.0.0.1:15173/admin/collabo/${sample.id}`);
      await phone.waitForSelector(".cb-editor");
      assert.equal(
        await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
        `editor width ${width}`,
      );
    }
    await page.route("**/api/collabo?*", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"Database unavailable"}' }),
    );
    await page.goto("http://127.0.0.1:15173/collabo");
    await page.getByRole("alert").filter({ hasText: "Database unavailable" }).waitFor();
    assert.equal(await page.locator(".cb-card").count(), 0);
    assert.deepEqual(browserErrors, []);
    console.log(
      "PASS: desktop/light/dark, pagination, gallery keyboard + close, editor draft export + mocked DB save, mobile layout, vertical-scroll guard, collaboration swipe, CD bidirectional swipe.",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
