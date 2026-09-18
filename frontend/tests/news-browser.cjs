// All APIs mocked. Headless only. Does not alter any local/test/production database.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "../..");
const port = 15174;
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
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==",
  "base64",
);
const article = {
  id: "one",
  title: "测试新闻",
  source: "niji_topics",
  source_label: "Official Site News",
  page_name: "01_123",
  source_url: "https://www.lovelive-anime.jp/news/01_123.html",
  published_at: "2026-09-11",
  tags: ["goods", "voice:radio"],
  summary: "正文示例",
  body_markdown:
    '###### 六级标题\n\n**加粗** 与 *斜体* ~~删除~~\n\n> 引用\n\n1. 第一项\n   - 嵌套项\n\n| 名称 | 内容 |\n|---|---|\n| A | B |\n\n```js\nconst a = 1;\n```\n\n[查询](/news?q=a&b=2)\n\n<img src=x onerror="window.bad=1"><script>window.bad=1</script>\n\n![正文图片](pic/a.png)',
  images: [
    {
      id: 1,
      url: "/api/news/images/1",
      local_path: "archive:pic/a.png",
      kind: "archive",
      alt_text: "第一张",
    },
    { id: 2, url: "/api/news/images/2", kind: "manual", alt_text: "第二张" },
  ],
  updated_at: "v1",
};
const deletedImageIds = new Set();
async function setup(context) {
  await context.addInitScript(() => {
    if (!localStorage.getItem("locale"))
      localStorage.setItem("locale", "zh-CN");
    localStorage.setItem("theme", "latte");
  });
  await context.route("https://**/*", (route) => route.abort());
  await context.route("**/api/**", async (route) => {
    const u = new URL(route.request().url());
    const p = u.pathname;
    const json = (data) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    if (p.startsWith("/api/news/images/"))
      return route.fulfill({ contentType: "image/png", body: png });
    if (p === "/api/auth/session") return json({ authenticated: true });
    if (p === "/api/news") {
      const requestedPage = Math.max(1, Number(u.searchParams.get("page")) || 1);
      return json({
        items: [{ ...article, image_count: 2 }],
        total: 48,
        pages: 2,
        page: Math.min(requestedPage, 2),
        tag_options: [{ name: "goods", count: 1 }],
        source_options: [
          { name: "official_site", label: "Official Site News", count: 1 },
          { name: "as_news", label: "AS News", count: 0 },
        ],
      });
    }
    if (p.startsWith("/api/news/")) {
      const id = p.split("/").pop();
      return json({
        article: {
          ...article,
          id,
          images: article.images.filter(
            (image) => !deletedImageIds.has(image.id),
          ),
        },
        previous: id === "two" ? { id: "one", title: "上一篇" } : null,
        following: id === "one" ? { id: "two", title: "下一篇" } : null,
      });
    }
    if (
      p.startsWith("/api/admin/news/images/") &&
      route.request().method() === "DELETE"
    ) {
      deletedImageIds.add(Number(p.split("/").pop()));
      return json({
        article: {
          ...article,
          images: article.images.filter(
            (image) => !deletedImageIds.has(image.id),
          ),
        },
      });
    }
    if (p === "/api/admin/news/one/images") return json({ article });
    if (p === "/api/admin/news/one")
      return json({
        article: { ...article, ...route.request().postDataJSON() },
      });
    if (p === "/api/admin/backups") return json({ backups: [] });
    if (p === "/api/admin/settings")
      return json({
        settings: {
          interval_minutes: "10",
          detail_interval_minutes: "5",
          music_auto_sync: "1",
          news_interval_minutes: "30",
          news_auto_sync: "1",
          news_slow_refresh_enabled: "0",
          news_slow_refresh_delay_seconds: "10",
        },
        news_slow_refresh: {
          total: 1,
          pending: 1,
          processing: 0,
          completed: 0,
          failed: 0,
          skipped: 0,
          remaining: 1,
          risk_failed: 0,
          failed_pages: [],
          last_page: null,
          running: false,
        },
        activity_logs: Array.from({ length: 40 }, (_, i) => ({
          id: i,
          checked_at: "2026-09-11T12:00:00Z",
          category: i % 2 ? "news" : "collabo",
          summary: "资料变动",
        })),
      });
    return json({});
  });
}
async function swipe(page, dx) {
  await page.locator(".news-detail-header h1").evaluate((el, dx) => {
    const point = (x) =>
      new Touch({ identifier: 1, target: el, clientX: x, clientY: 220 });
    el.dispatchEvent(
      new TouchEvent("touchstart", { bubbles: true, touches: [point(160)] }),
    );
    el.dispatchEvent(
      new TouchEvent("touchend", {
        bubbles: true,
        touches: [],
        changedTouches: [point(160 + dx)],
      }),
    );
  }, dx);
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
      viewport: { width: 1440, height: 1000 },
    });
    await setup(context);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(base + "/news?source=official_site");
    await page.getByRole("button", { name: "Official Site News 1" }).waitFor();
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight - innerHeight));
    await page.getByRole("button", { name: /下一页/ }).click();
    await page.waitForURL(/page=2/);
    await page.locator(".news-card-index").waitFor();
    assert.equal(await page.locator(".news-card-index").first().innerText(), "025");
    assert.ok((await page.evaluate(() => window.scrollY)) < 100, "reset news pagination scroll");
    await page.goto(base + "/news?source=official_site");
    await page.getByRole("button", { name: "Official Site News 1" }).waitFor();
    assert.deepEqual(
      await page.locator(".section-nav a").evaluateAll((links) =>
        links.map((link) => link.textContent.trim()),
      ),
      ["音乐档案", "节目档案", "联动立绘", "官网新闻"],
    );
    assert.ok(await page.getByRole("button", { name: "#周边 1" }).isVisible());
    await page.evaluate(() => window.scrollTo(0, Math.min(700, document.documentElement.scrollHeight - innerHeight)));
    const tagScroll = await page.evaluate(() => window.scrollY);
    await page.getByRole("button", { name: "#周边 1" }).click();
    await page.waitForURL(/tags=goods/);
    await page.waitForTimeout(150);
    assert.ok(Math.abs((await page.evaluate(() => window.scrollY)) - tagScroll) < 2, "preserve tag filter scroll");
    assert.equal(await page.getByRole("button", { name: "清除筛选", exact: true }).count(), 1);
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByText("管理新闻标签").waitFor();
    await page.getByRole("button", { name: "关闭", exact: true }).first().click();
    await page.goto(base + "/news/one?source=official_site&page=2");
    await page.locator(".news-detail-copy h6").waitFor();
    assert.equal(await page.title(), "测试新闻 · Nijigasaki DB");
    for (const tag of [
      "strong",
      "em",
      "del",
      "blockquote",
      "ol",
      "table",
      "pre code",
    ])
      assert.ok(await page.locator(".news-detail-copy " + tag).count(), tag);
    assert.equal(await page.evaluate(() => window.bad), undefined);
    assert.ok(
      (
        await page.getByRole("link", { name: "查询" }).getAttribute("href")
      ).includes("?q=a&b=2"),
    );
    const inline = page.locator(
      '.news-detail-copy img[src*="/api/news/images/1"]',
    );
    assert.equal(await inline.getAttribute("loading"), "lazy");
    await inline.click();
    await page.locator("dialog[open]").waitFor();
    await page.keyboard.press("ArrowRight");
    assert.ok(
      (
        await page.locator(".news-lightbox-stage img").getAttribute("src")
      ).endsWith("/api/news/images/2"),
    );
    assert.ok(page.url().includes("/news/one"));
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("dialog[open]").count(), 0);
    await page.evaluate(() => document.activeElement.blur());
    await page.keyboard.press("ArrowRight");
    await page.waitForURL("**/news/two?*");
    await page.locator(".news-detail-copy h6").waitFor();
    assert.ok(page.url().includes("source=official_site"));
    await page.keyboard.press("ArrowLeft");
    await page.waitForURL("**/news/one?*");
    await page.locator(".news-detail-copy h6").waitFor();
    assert.equal(
      await page.getByRole("button", { name: "从官网刷新此条" }).count(),
      0,
    );
    await page.getByRole("button", { name: "编辑该页" }).click();
    assert.equal(await page.locator(".news-edit-tag").count(), 34);
    const goodsTag = page.locator(".news-edit-tag").filter({ hasText: "#周边" });
    const musicTag = page.locator(".news-edit-tag").filter({ hasText: "#音乐" });
    assert.equal(await goodsTag.getAttribute("aria-pressed"), "true");
    await musicTag.click();
    assert.equal(await musicTag.getAttribute("aria-pressed"), "true");
    await musicTag.click();
    await page.locator(".news-edit-form textarea").first().focus();
    await page.keyboard.press("ArrowRight");
    assert.ok(page.url().includes("/news/one"));
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByRole("button", { name: "编辑该页" }).click();
    page.once("dialog", (dialog) => dialog.accept());
    await page
      .locator(".news-existing-image")
      .first()
      .getByRole("button", { name: "删除" })
      .click();
    await page.getByText("图片已删除").waitFor();
    assert.equal(await page.locator(".news-existing-image").count(), 1);
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await page.goto(base + "/admin?section=music");
    const musicSettings = page.locator("form.settings-card").filter({ hasText: "音乐抓取设置" });
    await musicSettings.waitFor();
    assert.equal(await musicSettings.locator('input[type="checkbox"]').count(), 1);
    assert.equal(await musicSettings.locator('input[type="checkbox"]').isChecked(), true);
    assert.ok((await musicSettings.innerText()).includes("启用音乐自动检查"));
    await page.goto(base + "/admin?section=news");
    await page.locator(".news-monitor-card").waitFor();
    assert.deepEqual(
      await page.locator(".settings-directory a").evaluateAll((links) =>
        links.map((link) => link.textContent.trim()),
      ),
      ["音乐抓取设置", "新闻抓取设置", "Bot 设置", "数据库", "账号安全"],
    );
    assert.equal(await page.locator(".news-slow-refresh-status").count(), 1);
    assert.equal(await page.locator(".password-form").isVisible(), false);
    assert.equal(
      await page.getByRole("button", { name: "打开官网新闻" }).count(),
      0,
    );
    await page.getByRole("link", { name: "Bot 设置", exact: true }).click();
    await page.locator(".bot-settings-card").waitFor();
    assert.equal(await page.locator(".bot-settings-card").isVisible(), true);
    assert.equal(await page.locator(".settings-directory a").count(), 5);
    await page.getByRole("link", { name: "数据库", exact: true }).click();
    await page.locator(".sync-log-list").waitFor();
    assert.equal(await page.locator(".sync-log-list li").count(), 15);
    assert.ok(
      (await page.locator(".sync-log-list").innerText()).includes(
        "联动立绘档案",
      ),
    );
    await page.locator(".settings-log-toolbar select").selectOption("news");
    assert.ok(
      !(await page.locator(".sync-log-list").innerText()).includes(
        "联动立绘档案",
      ),
    );
    for (const width of [320, 390, 768]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(base + "/admin?section=database");
      await page.locator(".sync-log-list").waitFor();
      assert.ok(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `settings overflow ${width}`,
      );
      await page.goto(base + "/news/one");
      await page.locator(".news-detail-copy").waitFor();
      assert.ok(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `news overflow ${width}`,
      );
      if (width <= 390) {
        const mobileOrder = await page.evaluate(() => ({
          galleryTop: document.querySelector(".news-detail-gallery")?.getBoundingClientRect().top,
          copyTop: document.querySelector(".news-detail-copy")?.getBoundingClientRect().top,
        }));
        assert.ok(
          mobileOrder.galleryTop < mobileOrder.copyTop,
          `news images should precede article text on mobile (${width})`,
        );
      }
    }
    await swipe(page, -100);
    await page.waitForURL("**/news/two");
    await page.locator(".news-detail-copy").waitFor();
    await swipe(page, 100);
    await page.waitForURL("**/news/one");
    await page.evaluate(() => {
      localStorage.setItem("locale", "ja");
    });
    await page.reload();
    await page.getByText("#グッズ", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log(
      "PASS: Markdown/GFM/XSS, inline/gallery lightbox, arrows/swipe, filters, edit guards, refresh, settings sections/log pagination, mobile widths, Japanese tags.",
    );
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
