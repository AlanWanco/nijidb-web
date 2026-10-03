import assert from "node:assert/strict";
import test from "node:test";
import { createCalendarCache, requestCalendarSnapshot } from "../src/programCalendarCache.js";
import { createCalendarReveal } from "../src/programCalendarReveal.js";

const payload = { programs: [{ id: "one", title: "公开节目" }], events: [{ id: "episode", start: "2026-10-03", title: "本期" }], start: "2026-10-01", end: "2026-10-31" };
const entry = { payload, etag: 'W/"one"', savedAt: 100 };

test("public cache works without storage, isolates callers, expires and bounds windows", async () => {
  let clock = 100;
  const cache = createCalendarCache({ storage: null, now: () => clock, maxEntries: 2, ageLimit: 100 });
  await cache.put("a", entry);
  const value = await cache.get("a");
  value.payload.events[0].title = "修改缓存副本";
  assert.equal((await cache.get("a")).payload.events[0].title, "本期");
  await cache.put("b", entry);
  await cache.get("a");
  await cache.put("c", entry);
  assert.equal(await cache.get("b"), null);
  assert.ok(await cache.get("a"));
  clock = 200;
  assert.equal(await cache.get("a"), null);
});

test("blocked or failing IndexedDB never prevents fetching a calendar", async () => {
  const failing = createCalendarCache({ storage: { open() { throw new Error("blocked"); } }, now: () => 100 });
  await failing.put("a", entry);
  assert.ok(await failing.get("a"));
  assert.equal(await failing.get("missing"), null);
  const hanging = createCalendarCache({ storage: { open() { return {}; } }, timeout: 5 });
  assert.equal(await hanging.get("missing"), null);
});

test("invalid data and unsafe ETags cannot enter conditional requests", async () => {
  const cache = createCalendarCache({ storage: null, now: () => 100 });
  await cache.put("invalid", { ...entry, payload: { events: "not-array", programs: [] } });
  assert.equal(await cache.get("invalid"), null);
  await cache.put("unsafe", { ...entry, etag: 'bad\r\nCookie: secret', adminSecret: "not-stored" });
  const cached = await cache.get("unsafe");
  assert.equal(cached.etag, null);
  assert.equal(Object.hasOwn(cached, "adminSecret"), false);
  await requestCalendarSnapshot("/api/programs/calendar", {
    cached,
    fetcher: async (path, options) => {
      assert.equal(options.headers["If-None-Match"], undefined);
      return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
    },
  });
});

test("304 reuses the snapshot without parsing a body and advances its validation time", async () => {
  const controller = new AbortController();
  const result = await requestCalendarSnapshot("/api/programs/calendar", {
    cached: entry, signal: controller.signal, now: () => 200,
    fetcher: async (path, options) => {
      assert.equal(path, "/api/programs/calendar");
      assert.equal(options.credentials, "same-origin");
      assert.equal(options.headers["If-None-Match"], entry.etag);
      assert.equal(options.signal, controller.signal);
      return new Response(null, { status: 304, headers: { ETag: entry.etag } });
    },
  });
  assert.equal(result.notModified, true);
  assert.equal(result.entry.savedAt, 200);
  assert.deepEqual(result.entry.payload, payload);
});

test("changed snapshots, no-store, missing cache, and API failures are distinct", async () => {
  const result = await requestCalendarSnapshot("/api/programs/calendar", {
    cached: entry,
    fetcher: async () => new Response(JSON.stringify(payload), { headers: { ETag: 'W/"two"', "Cache-Control": "no-store" } }),
  });
  assert.equal(result.notModified, false);
  assert.equal(result.entry.etag, 'W/"two"');
  assert.equal(result.cacheable, false);
  await assert.rejects(() => requestCalendarSnapshot("/api/programs/calendar", {
    fetcher: async () => new Response(null, { status: 304 }),
  }), /缓存已失效/);
  await assert.rejects(() => requestCalendarSnapshot("/api/programs/calendar", {
    fetcher: async () => new Response(JSON.stringify({ detail: "暂不可用" }), { status: 503 }),
  }), error => error.status === 503 && error.message === "暂不可用");
});

test("a cached or fetched snapshot cannot belong to another month window", async () => {
  const path = "/api/programs/calendar?start=2026-10-01&end=2026-11-01";
  const cache = createCalendarCache({ storage: null, now: () => 100 });
  await cache.put(path, { ...entry, payload: { ...payload, start: "2026-09-01", end: "2026-09-30" } });
  assert.equal(await cache.get(path), null);
  await cache.put(path, entry);
  assert.ok(await cache.get(path));
  await assert.rejects(() => requestCalendarSnapshot(path, {
    fetcher: async () => new Response(JSON.stringify({ ...payload, start: "2026-09-01" })),
  }), /数据格式无效/);
});

function fakeElement() {
  const classes = new Set();
  const properties = new Map();
  const listeners = new Map();
  return {
    classes, properties, listeners,
    classList: { add: value => classes.add(value), remove: (...values) => values.forEach(value => classes.delete(value)) },
    style: { setProperty: (key, value) => properties.set(key, value), removeProperty: key => properties.delete(key) },
    addEventListener: (type, handler) => listeners.set(type, handler),
    removeEventListener: (type, handler) => { if (listeners.get(type) === handler) listeners.delete(type); },
  };
}

function fakeView(reduce = false) {
  const motion = { matches: reduce, addEventListener(type, handler) { this.change = handler; }, removeEventListener() { this.change = null; } };
  const view = {
    motion, observed: new Set(), callback: null,
    matchMedia: () => motion,
    IntersectionObserver: class {
      constructor(callback) { view.callback = callback; }
      observe(element) { view.observed.add(element); }
      unobserve(element) { view.observed.delete(element); }
      disconnect() { view.observed.clear(); }
    },
  };
  return view;
}

test("only visible nodes reveal, and remounting the same event does not replay", () => {
  const view = fakeView();
  const reveal = createCalendarReveal(view);
  const first = fakeElement();
  const second = fakeElement();
  reveal.register(first, "one");
  reveal.register(second, "two");
  assert.ok(first.classes.has("program-event-reveal-pending"));
  view.callback([{ target: first, isIntersecting: true, boundingClientRect: { top: 0, left: 0 } }]);
  assert.ok(first.classes.has("program-event-enter"));
  assert.ok(second.classes.has("program-event-reveal-pending"));
  first.listeners.get("animationend")();
  assert.equal(first.classes.has("program-event-enter"), false);
  reveal.unregister(first);
  const remounted = fakeElement();
  reveal.register(remounted, "one");
  assert.equal(remounted.classes.size, 0);
  assert.equal(view.observed.has(remounted), false);
  reveal.destroy();
  assert.equal(second.classes.size, 0);
  assert.equal(view.observed.size, 0);
});

test("reduced motion, keyboard focus and missing observers never leave content hidden", () => {
  const reduced = fakeView(true);
  const reveal = createCalendarReveal(reduced);
  const element = fakeElement();
  reveal.register(element, "one");
  assert.equal(element.classes.size, 0);
  reduced.motion.matches = false;
  const another = fakeElement();
  reveal.register(another, "two");
  another.listeners.get("focusin")();
  assert.equal(another.classes.has("program-event-reveal-pending"), false);
  const third = fakeElement();
  reveal.register(third, "three");
  reduced.motion.matches = true;
  reduced.motion.change();
  assert.equal(third.classes.size, 0);
  reveal.destroy();
  const fallback = createCalendarReveal({});
  const plain = fakeElement();
  fallback.register(plain, "plain");
  assert.equal(plain.classes.size, 0);
  fallback.destroy();
});
