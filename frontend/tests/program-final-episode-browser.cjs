// Final-episode controls and period-end synchronization, with mocked APIs only.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15181;
const base = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, [
  "frontend/node_modules/vite/bin/vite.js", "frontend", "--host", "127.0.0.1", "--port", String(port), "--strictPort",
], { cwd: root, stdio: "ignore" });

async function setup(context, { status = "rescheduled", fail = false, firstDelay = 0 } = {}) {
  const period = { id: 1, start_date: "2026-01-01", end_date: "", frequency: "individual", auto_generate: false, timezone: "Asia/Tokyo" };
  const program = {
    id: "final-test", title: "完结期测试节目", category: "personal", format: "video", platform: "network",
    delivery: "recorded", status: "ongoing", auto_generate: false, episode_start: 1, people: [],
    start_date: period.start_date, end_date: "", periods: [period], episode_count: 1,
  };
  let occurrence = {
    id: 1, original_date: "2026-02-03", original_time: "20:00", date: "2026-02-10", time: "20:00",
    status, adjusted_date: status === "rescheduled" ? "2026-02-10" : "", adjusted_time: "",
    is_final: false, individual: true, generated: false, episode: 1, timezone: "Asia/Tokyo", guests: [], absent_members: [], images: [],
  };
  const mutations = [];
  await context.addInitScript(() => {
    localStorage.setItem("locale", "zh-CN");
    localStorage.setItem("theme", "latte");
  });
  await context.route("https://**/*", route => route.abort());
  await context.route("**/api/**", async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const json = (data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    if (pathname === "/api/auth/session") return json({ authenticated: true, role: "admin" });
    if (pathname === "/api/programs") return json({ programs: [program] });
    if (pathname === "/api/admin/programs/final-test/occurrences" && request.method() === "GET") return json({ occurrences: [occurrence] });
    if (pathname === "/api/admin/programs/final-test/occurrences/1" && request.method() === "PATCH") {
      const body = request.postDataJSON();
      mutations.push(body);
      if (mutations.length === 1 && firstDelay) await new Promise(resolve => setTimeout(resolve, firstDelay));
      if (fail) return json({ detail: "测试保存失败" }, 409);
      occurrence = { ...occurrence, ...body, date: body.status === "rescheduled" ? body.adjusted_date : body.original_date };
      if (occurrence.is_final) {
        period.end_date = occurrence.date;
        program.end_date = occurrence.date;
        program.status = "completed";
      }
      return json({ occurrence });
    }
    return json({});
  });
  return { mutations };
}

(async () => {
  let browser;
  try {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try { if ((await fetch(base)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      const { mutations } = await setup(context);
      const page = await context.newPage();
      await page.goto(`${base}/admin/programs?program=final-test&panel=occurrences&occurrence=1`);
      const button = page.getByRole("button", { name: "本集完结", exact: true });
      await button.waitFor();
      assert.equal(await button.isDisabled(), false);
      assert.equal(await button.evaluate(element => element.previousElementSibling.textContent.trim()), "清空列表");
      assert.equal(await button.evaluate(element => element.classList.contains("program-action-button")), true);
      const clearBox = await page.getByRole("button", { name: "清空列表", exact: true }).boundingBox();
      const finalBox = await button.boundingBox();
      if (width === 1440) {
        assert.ok(finalBox.x >= clearBox.x + clearBox.width);
        assert.ok(Math.abs(finalBox.y - clearBox.y) < 1);
      }
      page.once("dialog", dialog => dialog.dismiss());
      await button.click();
      assert.equal(mutations.length, 0);

      // Pending metadata in the program panel must survive the episode mutation.
      await page.getByRole("button", { name: "切换到上一个工作区" }).click();
      await page.getByLabel("节目名称", { exact: true }).fill("尚未保存的节目名称");
      await page.getByRole("button", { name: "切换到下一个工作区" }).click();
      let confirmation = "";
      page.once("dialog", async dialog => {
        confirmation = dialog.message();
        await dialog.accept();
      });
      await page.getByRole("button", { name: "本集完结", exact: true }).click();
      await page.getByRole("button", { name: "本集已完结", exact: true }).waitFor();
      assert.match(confirmation, /2026-02-10/);
      assert.equal(mutations.length, 1);
      assert.equal(mutations[0].is_final, true);
      assert.equal(mutations[0].adjusted_date, "2026-02-10");
      assert.equal(await page.getByRole("button", { name: "本集已完结", exact: true }).isDisabled(), true);
      await page.getByRole("button", { name: "切换到上一个工作区" }).click();
      assert.equal(await page.getByLabel("节目名称", { exact: true }).inputValue(), "尚未保存的节目名称");
      assert.equal(await page.locator(".period-end-field input").inputValue(), "2026-02-10");
      await context.close();
    }
    for (const status of ["cancelled", "deleted"]) {
      const context = await browser.newContext();
      await setup(context, { status });
      const page = await context.newPage();
      await page.goto(`${base}/admin/programs?program=final-test&panel=occurrences&occurrence=1`);
      const button = page.getByRole("button", { name: "本集完结", exact: true });
      await button.waitFor();
      assert.equal(await button.isDisabled(), true);
      await context.close();
    }
    const raceContext = await browser.newContext();
    const { mutations: racingMutations } = await setup(raceContext, { firstDelay: 1000 });
    const racePage = await raceContext.newPage();
    racePage.on("dialog", dialog => dialog.accept());
    await racePage.goto(`${base}/admin/programs?program=final-test&panel=occurrences&occurrence=1`);
    await racePage.getByRole("button", { name: "本集完结", exact: true }).click();
    await racePage.getByLabel("备注", { exact: true }).fill("请求途中编辑的备注");
    for (let attempt = 0; attempt < 50 && racingMutations.length < 2; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(racingMutations.length, 2);
    assert.equal(racingMutations[0].is_final, true);
    assert.equal(racingMutations[1].is_final, true);
    assert.equal(racingMutations[1].note, "请求途中编辑的备注");
    await raceContext.close();

    const context = await browser.newContext();
    const { mutations } = await setup(context, { fail: true });
    const page = await context.newPage();
    page.on("dialog", dialog => dialog.accept());
    await page.goto(`${base}/admin/programs?program=final-test&panel=occurrences&occurrence=1`);
    const button = page.getByRole("button", { name: "本集完结", exact: true });
    await button.click();
    await page.getByRole("alert").waitFor();
    assert.equal(mutations.length, 1);
    assert.equal(await button.isDisabled(), false);
    assert.equal(await page.getByRole("button", { name: "本集已完结", exact: true }).count(), 0);
    await context.close();
    console.log("PASS: desktop/mobile final button, confirmation, real date, period-end sync, metadata retention, disabled states, queued auto-save races, and failed saves.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
